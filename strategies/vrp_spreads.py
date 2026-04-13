"""Volatility Risk Premium (VRP) harvesting via put credit spreads.

The most empirically validated options strategy — 32+ years of evidence
from the CBOE PUT Index (Sharpe 0.65 vs. 0.49 for equities).

MECHANISM:
  Implied volatility consistently exceeds realized volatility because
  institutions structurally buy puts for portfolio insurance. This creates
  a persistent premium for those willing to sell that insurance.

STRUCTURE:
  Sell a put credit spread (bull put spread) on SPY:
  - Sell the short put at ~16 delta (~1 std dev OTM, ~84% prob of profit)
  - Buy a long put $5 below for protection (caps max loss)
  - Collect credit upfront
  - Close at 50% of max profit (don't wait for expiration)

EVIDENCE:
  - CBOE PUT Index: 9.54% annual return, 9.95% std dev (1986-2018)
  - vs. S&P 500: 9.80% return, 14.93% std dev — similar return, 1/3 less risk
  - The premium persists because institutional insurance demand is structural
  - Survived 1987, 2001, 2008, 2018 Volmageddon, 2020 COVID

RISK:
  - Max loss per spread is defined (spread width - credit received)
  - Worst case at $5K: 25-40% drawdown in a severe crash
  - Expected recovery: 3-9 months historically
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

from scipy.stats import norm

from data.models import Signal, SignalAction, Strategy, Urgency
from utils.config import get
from utils.logger import get_logger
from utils.market import dte, has_earnings_within
from utils.timing import now_et

log = get_logger(__name__)

# ── P2-14: Sector map for correlation check ──────────────────────────────

SECTOR_MAP = {
    'AAPL': 'tech', 'MSFT': 'tech', 'NVDA': 'tech', 'GOOGL': 'tech', 'META': 'tech', 'AMD': 'tech', 'CRM': 'tech',
    'TSLA': 'consumer', 'AMZN': 'consumer', 'NFLX': 'consumer', 'DIS': 'consumer', 'NKE': 'consumer',
    'JPM': 'finance', 'BAC': 'finance', 'GS': 'finance', 'V': 'finance', 'MA': 'finance', 'XLF': 'finance', 'COIN': 'finance',
    'SPY': 'index', 'QQQ': 'index', 'IWM': 'index', 'DIA': 'index',
    'XLE': 'energy', 'XLK': 'tech', 'XLV': 'health', 'XLI': 'industrial',
    'PLTR': 'tech', 'SOFI': 'finance', 'SHOP': 'tech', 'ROKU': 'tech', 'SNAP': 'tech',
    'BA': 'industrial', 'CAT': 'industrial', 'HD': 'consumer',
    'GDX': 'commodity', 'TLT': 'bond', 'HYG': 'bond', 'EEM': 'emerging',
    'UBER': 'tech', 'ABNB': 'consumer', 'DKNG': 'consumer', 'HOOD': 'finance',
    'MARA': 'crypto', 'RIOT': 'crypto', 'SQ': 'finance',
    'PFE': 'health',
}

# Risk-free rate for Black-Scholes calculations
_RISK_FREE_RATE = 0.05


def _estimate_delta(stock_price: float, strike: float, dte_days: int, iv: float, option_type: str = 'put') -> float:
    """Estimate option delta using Black-Scholes.

    Parameters:
        stock_price: Current underlying price.
        strike: Option strike price.
        dte_days: Days to expiration.
        iv: Implied volatility as a decimal (e.g. 0.25 for 25%).
        option_type: 'put' or 'call'.

    Returns:
        Delta value (negative for puts, positive for calls).
    """
    if dte_days <= 0 or iv <= 0 or stock_price <= 0 or strike <= 0:
        return -0.16 if option_type == 'put' else 0.50

    T = dte_days / 365.0
    r = _RISK_FREE_RATE
    sigma = iv

    d1 = (math.log(stock_price / strike) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))

    if option_type == 'put':
        return norm.cdf(d1) - 1.0
    return norm.cdf(d1)


def _estimate_iv_from_price(
    stock_price: float,
    strike: float,
    dte_days: int,
    option_mid_price: float,
    option_type: str = 'put',
) -> float:
    """Estimate implied volatility from the option mid-price using bisection.

    Falls back to 0.25 if the solver doesn't converge.
    """
    if option_mid_price <= 0 or dte_days <= 0 or stock_price <= 0 or strike <= 0:
        return 0.25

    T = dte_days / 365.0
    r = _RISK_FREE_RATE

    def _bs_price(sigma: float) -> float:
        d1 = (math.log(stock_price / strike) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if option_type == 'put':
            return strike * math.exp(-r * T) * norm.cdf(-d2) - stock_price * norm.cdf(-d1)
        return stock_price * norm.cdf(d1) - strike * math.exp(-r * T) * norm.cdf(d2)

    lo, hi = 0.01, 5.0
    for _ in range(64):
        mid = (lo + hi) / 2.0
        price = _bs_price(mid)
        if price < option_mid_price:
            lo = mid
        else:
            hi = mid
        if abs(hi - lo) < 1e-6:
            break

    estimated = (lo + hi) / 2.0
    # Sanity check: if the result is extreme, fall back to a reasonable default
    if estimated < 0.02 or estimated > 4.0:
        return 0.25
    return estimated


class VRPSpreadStrategy:
    """Systematic put credit spread selling on SPY to harvest the volatility risk premium."""

    def __init__(self, broker, db):
        self.broker = broker
        self.db = db

        # Load parameters from config
        self.underlyings = get("vrp_spreads.underlyings", ["SPY"])
        if isinstance(self.underlyings, str):
            self.underlyings = [self.underlyings]
        self.spread_width = get("vrp_spreads.spread_width", 5.0)
        self.target_delta = get("vrp_spreads.target_delta", 0.16)
        self.delta_min = get("vrp_spreads.delta_min", 0.10)
        self.delta_max = get("vrp_spreads.delta_max", 0.22)
        self.target_dte_min = get("vrp_spreads.target_dte_min", 30)
        self.target_dte_max = get("vrp_spreads.target_dte_max", 45)
        self.profit_target_pct = get("vrp_spreads.profit_target_pct", 0.50)
        self.stop_loss_multiplier = get("vrp_spreads.stop_loss_multiplier", 2.0)
        self.max_spreads = get("vrp_spreads.max_simultaneous", 2)
        self.min_credit = get("vrp_spreads.min_credit", 0.50)
        self.min_open_interest = get("vrp_spreads.min_open_interest", 500)
        self.open_frequency_days = get("vrp_spreads.open_frequency_days", 7)
        self.vix_min = get("vrp_spreads.vix_min", 15.0)
        self.vix_max = get("vrp_spreads.vix_max", 40.0)
        self.spread_width_pct = get("vrp_spreads.spread_width_pct", 0.01)

    def scan_for_entries(self) -> list[Signal]:
        """Scan ALL underlyings for the best put credit spread opportunities.

        Scans SPY, QQQ, IWM, individual stocks — picks the best risk/reward
        spread across the entire universe.
        """
        signals = []

        # VIX gate — check if volatility is in the sweet spot
        vix_level = self._get_vix()
        if vix_level is not None:
            if vix_level < self.vix_min:
                log.info(
                    "VIX at %.1f < %.1f minimum. Premiums too thin — sitting on hands.",
                    vix_level, self.vix_min,
                )
                return signals
            if vix_level > self.vix_max:
                log.warning(
                    "VIX at %.1f > %.1f maximum. Market in panic — too risky to sell premium.",
                    vix_level, self.vix_max,
                )
                return signals
            log.info("VIX at %.1f — in the sweet spot (%.0f-%.0f). Proceeding with scan.",
                     vix_level, self.vix_min, self.vix_max)

        # Check position limit
        open_spreads = self.db.get_open_positions(strategy="vrp_spread")
        if len(open_spreads) >= self.max_spreads * 2:  # Each spread = 2 legs
            log.info("At max spread limit (%d/%d pairs). Skipping scan.",
                     len(open_spreads) // 2, self.max_spreads)
            return signals

        # Check if we opened a spread too recently
        if open_spreads:
            latest_entry = max(p.entry_date for p in open_spreads)
            days_since = (now_et().date() - datetime.strptime(latest_entry, "%Y-%m-%d").date()).days
            if days_since < self.open_frequency_days:
                log.info("Last spread opened %d days ago (min %d). Waiting.",
                         days_since, self.open_frequency_days)
                return signals

        # Scan ALL underlyings, collect best spread from each
        all_candidates = []
        for underlying in self.underlyings:
            spread = self._scan_single_underlying(underlying)
            if spread:
                all_candidates.append(spread)

        if not all_candidates:
            log.info("No qualifying spreads found across %d underlyings", len(self.underlyings))
            return signals

        # ── P2-14: Sector correlation check ──────────────────────────────
        # Penalize candidates whose sector already has an open position.
        open_sectors: set[str] = set()
        open_underlyings: set[str] = set()
        for pos in open_spreads:
            open_underlyings.add(pos.symbol)
            sector = SECTOR_MAP.get(pos.symbol)
            if sector:
                open_sectors.add(sector)

        for cand in all_candidates:
            underlying = cand["underlying"]
            # Never allow two spreads in the same underlying
            if underlying in open_underlyings:
                cand["score"] = -1.0
                continue
            sector = SECTOR_MAP.get(underlying)
            if sector and sector in open_sectors:
                cand["score"] *= 0.5

        # Remove disqualified candidates and sort by score
        all_candidates = [c for c in all_candidates if c["score"] > 0]
        all_candidates.sort(key=lambda s: s["score"], reverse=True)

        # Take top N (up to max_spreads available slots)
        slots_available = self.max_spreads - (len(open_spreads) // 2)
        for s in all_candidates[:slots_available]:
            signal = self._build_signal(s)
            signals.append(signal)
            log.info(
                "VRP signal: %s $%s/$%s put spread, %d DTE, credit $%.0f, score %.4f",
                s["underlying"], s["short_strike"], s["long_strike"],
                s["dte"], s["credit_total"], s["score"],
            )

        return signals

    def _scan_single_underlying(self, underlying: str) -> dict | None:
        """Scan one underlying for the best put credit spread. Returns best spread dict or None."""
        quote = self.broker.get_stock_quote(underlying)
        if not quote:
            log.debug("Could not get %s quote — skipping", underlying)
            return None

        spy_price = quote.price
        log.info("Scanning %s for put spread (price: $%.2f)", underlying, spy_price)

        # Fetch option chain — use direct Alpaca call for precise control
        from datetime import timedelta
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.data.requests import OptionLatestQuoteRequest

        today = now_et().date()
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status="active",
            expiration_date_gte=(today + timedelta(days=self.target_dte_min)).isoformat(),
            expiration_date_lte=(today + timedelta(days=self.target_dte_max)).isoformat(),
            type="put",
        )

        try:
            result = self.broker.trading.get_option_contracts(req)
            contracts = result.option_contracts if result else []
        except Exception as e:
            log.debug("Failed to fetch %s options: %s", underlying, e)
            return None

        if not contracts:
            log.debug("No put contracts for %s (%d-%d DTE)", underlying, self.target_dte_min, self.target_dte_max)
            return None

        # Filter to strikes in the ~4-10% OTM range (where 16 delta lives)
        target_low = spy_price * 0.90
        target_high = spy_price * 0.96
        filtered = [c for c in contracts if target_low <= float(c.strike_price) <= target_high]

        if not filtered:
            log.debug("No strikes in target range for %s", underlying)
            return None

        # Get live quotes
        symbols = [c.symbol for c in filtered[:50]]
        try:
            quotes_result = self.broker.option_data.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=symbols)
            )
        except Exception as e:
            log.debug("Failed to get option quotes for %s: %s", underlying, e)
            return None

        # Build puts list with live bid/ask
        from broker.models import OptionContract as OC
        puts = []
        for c in filtered:
            q = quotes_result.get(c.symbol)
            if not q or not q.bid_price or float(q.bid_price) <= 0:
                continue
            exp_str = c.expiration_date.isoformat() if c.expiration_date else ""
            puts.append(OC(
                symbol=c.symbol, option_type="put",
                strike=float(c.strike_price), expiration_date=exp_str,
                bid=float(q.bid_price),
                ask=float(q.ask_price) if q.ask_price else 0,
                mark=(float(q.bid_price) + (float(q.ask_price) if q.ask_price else 0)) / 2,
                delta=None, open_interest=0,
            ))

        if not puts:
            return None

        log.info("%s: %d puts with live quotes", underlying, len(puts))

        # ── P2-13: Dynamic spread width proportional to stock price ─────
        dynamic_width = max(1.0, min(self.spread_width, round(spy_price * self.spread_width_pct / 0.5) * 0.5))

        # Group by expiration, find best spread
        by_expiry: dict[str, list] = {}
        for p in puts:
            by_expiry.setdefault(p.expiration_date, []).append(p)

        best_spread = None
        best_score = -1.0

        for exp_date, exp_contracts in by_expiry.items():
            exp_contracts.sort(key=lambda c: c.strike, reverse=True)
            contract_dte = dte(exp_date)

            for short_put in exp_contracts:
                # ── P2-12 + P2-15: Estimate IV from option mid-price ─────
                short_mid = (short_put.bid + short_put.ask) / 2 if short_put.ask > 0 else short_put.bid
                estimated_iv = _estimate_iv_from_price(
                    spy_price, short_put.strike, contract_dte, short_mid, option_type='put',
                )

                # ── P2-12: Real delta via Black-Scholes ──────────────────
                abs_delta = abs(_estimate_delta(
                    spy_price, short_put.strike, contract_dte, estimated_iv, option_type='put',
                ))

                if not (self.delta_min <= abs_delta <= self.delta_max):
                    continue

                # ── P2-13: Find long put using dynamic_width ─────────────
                long_strike = short_put.strike - dynamic_width
                long_put = next(
                    (c for c in exp_contracts
                     if abs(c.strike - long_strike) < 0.5 and c.expiration_date == exp_date),
                    None,
                )
                if not long_put:
                    continue

                actual_width = short_put.strike - long_put.strike
                credit = short_put.bid - long_put.ask
                max_loss = actual_width - credit

                if credit < self.min_credit or max_loss <= 0:
                    continue

                # ── P2-16: Fixed scoring formula ─────────────────────────
                risk_reward = credit / max_loss
                risk_reward_score = min(risk_reward / 0.30, 1.0)
                delta_score = 1 - abs(abs_delta - self.target_delta) / 0.10
                dte_score = 1 - abs(contract_dte - 37) / 15
                iv_score = min(estimated_iv / 0.40, 1.0)  # P2-15: higher IV = better

                score = (
                    risk_reward_score * 0.30
                    + delta_score * 0.25
                    + dte_score * 0.15
                    + iv_score * 0.30
                )

                if score > best_score:
                    best_score = score
                    best_spread = {
                        "underlying": underlying,
                        "price": spy_price,
                        "short_strike": short_put.strike,
                        "long_strike": long_put.strike,
                        "expiration": exp_date,
                        "dte": contract_dte,
                        "credit": credit,
                        "credit_total": credit * 100,
                        "max_loss": max_loss,
                        "max_loss_total": max_loss * 100,
                        "risk_reward": risk_reward,
                        "delta": abs_delta,
                        "estimated_iv": estimated_iv,
                        "profit_target": credit * self.profit_target_pct,
                        "stop_loss_price": credit * self.stop_loss_multiplier,
                        "otm_pct": (spy_price - short_put.strike) / spy_price * 100,
                        "score": round(score, 4),
                    }

        if best_spread:
            log.info(
                "%s best: $%s/$%s put, credit $%.0f, score %.4f",
                underlying, best_spread["short_strike"], best_spread["long_strike"],
                best_spread["credit_total"], best_spread["score"],
            )
        return best_spread

    def _build_signal(self, s: dict) -> Signal:
        """Build a Signal from a spread dict."""
        return Signal(
            symbol=s["underlying"],
            strategy="vrp_spread",
            action=SignalAction.SELL_CSP.value,
            option_type="put",
            strike=s["short_strike"],
            expiration_date=s["expiration"],
            limit_price=s["credit"],
            estimated_credit=s["credit_total"],
            reason=(
                f"VRP put spread on {s['underlying']}: "
                f"Sell ${s['short_strike']:.0f}P / Buy ${s['long_strike']:.0f}P "
                f"({s['dte']} DTE, delta {s['delta']:.2f}, {s['otm_pct']:.1f}% OTM)\n"
                f"Credit: ${s['credit_total']:.0f} | Max loss: ${s['max_loss_total']:.0f} | "
                f"Risk/reward: {s['risk_reward']:.2f}\n"
                f"50% target: ${s['profit_target']*100:.0f} profit | "
                f"Stop: ${s['stop_loss_price']*100:.0f} loss"
            ),
            urgency=Urgency.NORMAL.value,
            optimal_execution_window="10:00-11:00 ET",
        )

    def check_exits(self, positions: list) -> list[Signal]:
        """Check open spreads for exit conditions.

        Rules:
        1. Close at 50% of max profit (credit * 0.50 = profit target)
        2. Close at 2x credit (stop loss)
        3. Close at DTE < 5 regardless (gamma risk)
        """
        signals = []

        # Group positions into spread pairs by pair_id
        spread_pairs = {}
        for pos in positions:
            if pos.strategy == "vrp_spread" and pos.pair_id:
                spread_pairs.setdefault(pos.pair_id, []).append(pos)

        for pair_id, legs in spread_pairs.items():
            short_leg = next((p for p in legs if p.option_type == "put" and p.entry_price > 0), None)
            long_leg = next((p for p in legs if p.option_type == "put" and p.entry_price <= 0), None)

            if not short_leg:
                continue

            # Skip if we don't have live pricing data — can't make exit decisions without it
            if short_leg.current_price is None:
                log.debug("Skipping spread %s — no live price data for short leg", pair_id)
                continue

            # Current spread value = short current - long current
            short_current = short_leg.current_price
            long_current = (long_leg.current_price or 0) if long_leg else 0
            spread_current_value = short_current - long_current

            entry_credit = short_leg.entry_price  # What we collected initially
            current_dte = short_leg.dte_remaining or 0

            # 4. Earnings check — close if earnings announced while in position
            if has_earnings_within(short_leg.symbol, 3):
                signals.append(Signal(
                    symbol=short_leg.symbol,
                    strategy="vrp_spread",
                    action=SignalAction.BUY_TO_CLOSE.value,
                    option_type="put",
                    strike=short_leg.strike,
                    expiration_date=short_leg.expiration_date,
                    limit_price=spread_current_value,
                    reason=f"EARNINGS within 3 days for {short_leg.symbol}! Closing to avoid gap risk.",
                    urgency=Urgency.URGENT.value,
                ))
                continue

            # 1. Profit target: spread has decayed to 50% of entry credit
            if entry_credit > 0 and spread_current_value <= entry_credit * (1 - self.profit_target_pct):
                profit = (entry_credit - spread_current_value) * 100
                signals.append(Signal(
                    symbol=short_leg.symbol,
                    strategy="vrp_spread",
                    action=SignalAction.BUY_TO_CLOSE.value,
                    option_type="put",
                    strike=short_leg.strike,
                    expiration_date=short_leg.expiration_date,
                    limit_price=spread_current_value,
                    estimated_pnl=profit,
                    reason=f"50% profit target hit! Entry: ${entry_credit:.2f} → Current: ${spread_current_value:.2f}. Profit: ${profit:.0f}",
                    urgency=Urgency.NORMAL.value,
                    status="auto_executed",  # Auto-execute profit targets
                ))
                continue

            # 2. Stop loss: spread has expanded to 2x entry credit
            if spread_current_value >= entry_credit * self.stop_loss_multiplier:
                loss = (spread_current_value - entry_credit) * 100
                signals.append(Signal(
                    symbol=short_leg.symbol,
                    strategy="vrp_spread",
                    action=SignalAction.BUY_TO_CLOSE.value,
                    option_type="put",
                    strike=short_leg.strike,
                    expiration_date=short_leg.expiration_date,
                    limit_price=spread_current_value,
                    estimated_pnl=-loss,
                    reason=f"STOP LOSS: Spread expanded to ${spread_current_value:.2f} (2x entry ${entry_credit:.2f}). Loss: ${loss:.0f}",
                    urgency=Urgency.URGENT.value,
                ))
                continue

            # 3. DTE < 5: close regardless to avoid gamma risk
            if current_dte < 5:
                signals.append(Signal(
                    symbol=short_leg.symbol,
                    strategy="vrp_spread",
                    action=SignalAction.BUY_TO_CLOSE.value,
                    option_type="put",
                    strike=short_leg.strike,
                    expiration_date=short_leg.expiration_date,
                    limit_price=spread_current_value,
                    reason=f"DTE < 5 ({current_dte} days). Closing to avoid gamma risk.",
                    urgency=Urgency.URGENT.value,
                ))

        return signals

    # ── Helpers ────────────────────────────────────────────────────────────

    def _get_vix(self) -> float | None:
        """Fetch current VIX level. Tries multiple approaches.

        Strategy:
          1. Try direct VIX index symbols on Alpaca (VIX, $VIX, ^VIX, VIX.X)
          2. Approximate from SPY near-ATM put implied volatility (IV ~ VIX/100)
          3. Fall back to VIXY ETF price with documented scaling limitation

        Returns the estimated VIX level, or None on total failure.
        """
        try:
            import math
            from alpaca.data.requests import StockLatestQuoteRequest, OptionLatestQuoteRequest
            from alpaca.trading.requests import GetOptionContractsRequest

            # ── Attempt 1: Direct VIX index symbols ──────────────────────
            for vix_symbol in ["VIX", "$VIX", "^VIX", "VIX.X"]:
                try:
                    req = StockLatestQuoteRequest(symbol_or_symbols=vix_symbol)
                    quotes = self.broker.stock_data.get_stock_latest_quote(req)
                    q = quotes.get(vix_symbol)
                    if q and q.bid_price and q.bid_price > 0:
                        vix_level = (q.bid_price + q.ask_price) / 2
                        log.info("VIX direct (%s): %.2f", vix_symbol, vix_level)
                        return vix_level
                except Exception:
                    continue
            log.debug("Direct VIX symbols unavailable — trying IV approximation")

            # ── Attempt 2: Approximate VIX from SPY put IV ───────────────
            # VIX ~ annualized implied volatility of near-ATM SPY options * 100
            try:
                spy_quote = self.broker.get_stock_quote("SPY")
                if spy_quote:
                    spy_price = spy_quote.price
                    today = now_et().date()
                    req = GetOptionContractsRequest(
                        underlying_symbols=["SPY"],
                        status="active",
                        type="put",
                        expiration_date_gte=(today + timedelta(days=25)).isoformat(),
                        expiration_date_lte=(today + timedelta(days=35)).isoformat(),
                    )
                    result = self.broker.trading.get_option_contracts(req)
                    contracts = result.option_contracts if result else []

                    # Find the nearest-ATM put
                    atm_puts = [
                        c for c in contracts
                        if abs(float(c.strike_price) - spy_price) / spy_price < 0.02
                    ]
                    if atm_puts:
                        best = min(atm_puts, key=lambda c: abs(float(c.strike_price) - spy_price))
                        oq_req = OptionLatestQuoteRequest(symbol_or_symbols=[best.symbol])
                        oq = self.broker.option_data.get_option_latest_quote(oq_req)
                        option_quote = oq.get(best.symbol)
                        if option_quote and option_quote.bid_price:
                            bid = float(option_quote.bid_price)
                            ask = float(option_quote.ask_price) if option_quote.ask_price else bid
                            mid = (bid + ask) / 2
                            strike_f = float(best.strike_price)
                            dte_days = (best.expiration_date - today).days if best.expiration_date else 30

                            # Rough IV approximation using Brenner-Subrahmanyam:
                            #   option_price ~ S * sigma * sqrt(T / (2*pi))
                            #   => sigma ~ option_price / (S * sqrt(T / (2*pi)))
                            # Then VIX ~ sigma * 100
                            t = max(dte_days, 1) / 365.0
                            iv_approx = mid / (strike_f * math.sqrt(t)) * math.sqrt(2 * math.pi)
                            vix_approx = iv_approx * 100
                            # Sanity bound: VIX between 9 and 80
                            if 9 <= vix_approx <= 80:
                                log.info(
                                    "VIX approximated from SPY put IV: %.2f "
                                    "(strike $%.0f, mid $%.2f, %d DTE)",
                                    vix_approx, strike_f, mid, dte_days,
                                )
                                return round(vix_approx, 2)
                            else:
                                log.debug(
                                    "IV-based VIX estimate %.2f outside sane range (9-80) — skipping",
                                    vix_approx,
                                )
            except Exception as e:
                log.debug("SPY IV approximation failed: %s", e)

            # ── Attempt 3: VIXY ETF price as last resort ─────────────────
            # LIMITATION: VIXY price != VIX level. VIXY tracks short-term
            # VIX futures and decays over time due to contango. The raw
            # price may diverge significantly from spot VIX.
            # Scaling factor: approximate VIX ~ VIXY_price * 1.0
            for vix_proxy in ["VIXY", "VXX", "UVXY"]:
                try:
                    req = StockLatestQuoteRequest(symbol_or_symbols=vix_proxy)
                    quotes = self.broker.stock_data.get_stock_latest_quote(req)
                    q = quotes.get(vix_proxy)
                    if q and q.bid_price and q.bid_price > 0:
                        price = (q.bid_price + q.ask_price) / 2
                        vix_estimate = price * 1.0
                        log.warning(
                            "VIX estimated from %s ETF: $%.2f -> VIX ~%.2f "
                            "(LIMITATION: %s tracks VIX futures, NOT spot VIX; "
                            "decays over time due to contango)",
                            vix_proxy, price, vix_estimate, vix_proxy,
                        )
                        return vix_estimate
                except Exception:
                    continue

            log.warning("Could not fetch VIX from any source — skipping VIX gate")
            return None
        except Exception as e:
            log.warning("VIX fetch error: %s — skipping gate", e)
            return None
