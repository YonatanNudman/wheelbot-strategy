"""Options Wheel Strategy — Cash-Secured Puts + Covered Calls on Alpaca.

THE WHEEL:
  1. SCANNING  — no position, looking for a CSP entry
  2. CSP_OPEN  — short put open, collecting theta
  3. SHARES_HELD — assigned on CSP, now holding 100 shares
  4. CC_OPEN   — covered call open against shares, collecting more theta
  5. (shares called away → back to SCANNING with accumulated premium)

Each symbol progresses independently through this cycle.  Multiple
symbols can be wheeling simultaneously up to the configured limit.

SAFETY RULES:
  - CSP strike * 100 must be affordable (within buying power allocation)
  - CC strike MUST be above the share cost basis (never lock in a loss)
  - Skip stocks with earnings within 14 days
  - Enforce delta, DTE, IVR, OI, and spread filters on every entry
"""

from __future__ import annotations

import math
from datetime import timedelta
from enum import Enum
from typing import Optional

from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.requests import GetOptionContractsRequest

from broker.models import OptionContract as BrokerContract
from data.models import (
    Position,
    Signal,
    SignalAction,
    SignalStatus,
    Strategy,
    Urgency,
)
from strategies.base import BaseStrategy
from strategies.rules import (
    check_cc_above_cost_basis,
    check_csp_delta,
    check_dte_range,
    check_profit_target,
    check_stop_loss,
    run_entry_checks,
    score_option,
)
from strategies.vrp_spreads import _estimate_delta, _estimate_iv_from_price
from utils.config import get
from utils.logger import get_logger
from utils.market import dte, has_earnings_within
from utils.timing import now_et

log = get_logger(__name__)


# ── Wheel State Machine ──────────────────────────────────────────────────

class WheelState(str, Enum):
    """Per-symbol state in the wheel cycle."""

    SCANNING = "scanning"
    CSP_OPEN = "csp_open"
    SHARES_HELD = "shares_held"
    CC_OPEN = "cc_open"


# ── Strategy ─────────────────────────────────────────────────────────────

class WheelStrategy(BaseStrategy):
    """Full wheel implementation: CSP -> assignment -> CC -> called away -> repeat.

    Relies on the Alpaca broker adapter for market data and order placement,
    and the database module for position tracking.
    """

    def __init__(self, broker, db_module) -> None:
        self.broker = broker
        self.db = db_module

        # ── Load config from wheel section ────────────────────────────────
        self.target_delta: float = get("wheel.target_delta", 0.20)
        self.cc_delta: float = get("wheel.cc_delta", 0.30)
        self.min_ivr: float = get("wheel.min_ivr", 30)
        self.target_dte_min: int = get("wheel.target_dte_min", 30)
        self.target_dte_max: int = get("wheel.target_dte_max", 45)
        self.profit_target_pct: float = get("wheel.profit_target_pct", 0.50)
        self.stop_loss_multiplier: float = get("wheel.stop_loss_multiplier", 2.0)
        self.roll_dte_threshold: int = get("wheel.roll_dte_threshold", 7)
        self.earnings_buffer_days: int = get("wheel.earnings_buffer_days", 14)
        self.min_open_interest: int = get("wheel.min_open_interest", 500)
        self.max_bid_ask_spread: float = get("wheel.max_bid_ask_spread", 0.10)

        # Capital and position limits
        self.max_per_position_pct: float = get("capital.max_per_position_pct", 0.50)
        self.max_wheel_positions: int = get("positions.max_wheel", 2)
        self.reserve_pct: float = get("capital.reserve_pct", 0.10)

        log.info(
            "WheelStrategy initialized: target_delta=%.2f, cc_delta=%.2f, "
            "DTE=[%d,%d], profit_target=%.0f%%, max_positions=%d",
            self.target_delta, self.cc_delta,
            self.target_dte_min, self.target_dte_max,
            self.profit_target_pct * 100, self.max_wheel_positions,
        )

    # ── State Map ────────────────────────────────────────────────────────

    def get_symbol_states(self) -> dict[str, WheelState]:
        """Build a map of symbol -> current wheel state from the database.

        Queries all open wheel positions and infers the state for each symbol:
          - wheel_csp open   -> CSP_OPEN
          - wheel_shares     -> SHARES_HELD
          - wheel_cc open    -> CC_OPEN
          - no position      -> SCANNING (not included in the map)
        """
        states: dict[str, WheelState] = {}

        csp_positions = self.db.get_open_positions(strategy=Strategy.WHEEL_CSP.value)
        for pos in csp_positions:
            states[pos.symbol] = WheelState.CSP_OPEN
            log.debug("State %s: CSP_OPEN (strike=%.2f, exp=%s)",
                      pos.symbol, pos.strike or 0, pos.expiration_date)

        cc_positions = self.db.get_open_positions(strategy=Strategy.WHEEL_CC.value)
        for pos in cc_positions:
            states[pos.symbol] = WheelState.CC_OPEN
            log.debug("State %s: CC_OPEN (strike=%.2f, exp=%s)",
                      pos.symbol, pos.strike or 0, pos.expiration_date)

        share_positions = self.db.get_open_positions(strategy=Strategy.WHEEL_SHARES.value)
        for pos in share_positions:
            # Only mark as SHARES_HELD if not already CC_OPEN
            # (CC_OPEN implies shares are held + a call is written)
            if pos.symbol not in states:
                states[pos.symbol] = WheelState.SHARES_HELD
                log.debug("State %s: SHARES_HELD (cost_basis=%.2f)",
                          pos.symbol, pos.cost_basis or 0)

        log.info("Wheel state map: %d symbols tracked — %s",
                 len(states), dict(states))
        return states

    # ── Entry Scanning ───────────────────────────────────────────────────

    def scan_for_entries(self, universe: list[str], broker: object | None = None) -> list[Signal]:
        """Scan the wishlist for new wheel entries based on current states.

        Args:
            universe: list of stock symbols (the wishlist)
            broker: optional override; defaults to self.broker

        For each symbol:
          SCANNING     -> find_csp_opportunity()
          SHARES_HELD  -> find_cc_opportunity()
          CSP_OPEN     -> skip (already have a position)
          CC_OPEN      -> skip (already have a position)

        Returns scored and ranked signals, limited to available capital slots.
        """
        signals: list[Signal] = []
        states = self.get_symbol_states()

        # Check global position limit
        open_wheel_count = self._count_active_wheel_symbols(states)
        if open_wheel_count >= self.max_wheel_positions:
            log.info("At max wheel positions (%d/%d). Only checking CC on held shares.",
                     open_wheel_count, self.max_wheel_positions)
            # Still check for CC opportunities on existing shares
            for symbol in universe:
                state = states.get(symbol, WheelState.SCANNING)
                if state == WheelState.SHARES_HELD:
                    signal = self._find_cc_opportunity(symbol)
                    if signal:
                        signals.append(signal)
            return signals

        # Check available capital
        buying_power = self.broker.get_buying_power()
        usable_capital = buying_power * (1.0 - self.reserve_pct)
        log.info("Buying power: $%.2f, usable (after %.0f%% reserve): $%.2f",
                 buying_power, self.reserve_pct * 100, usable_capital)

        for symbol in universe:
            state = states.get(symbol, WheelState.SCANNING)

            if state == WheelState.SCANNING:
                signal = self._find_csp_opportunity(symbol, usable_capital)
                if signal:
                    signals.append(signal)

            elif state == WheelState.SHARES_HELD:
                signal = self._find_cc_opportunity(symbol)
                if signal:
                    signals.append(signal)

            elif state in (WheelState.CSP_OPEN, WheelState.CC_OPEN):
                log.debug("Skipping %s — already in state %s", symbol, state.value)

        # Sort by estimated credit descending (best premium first)
        signals.sort(key=lambda s: s.estimated_credit or 0, reverse=True)

        log.info("Wheel scan complete: %d signals generated from %d symbols",
                 len(signals), len(universe))
        return signals

    # ── CSP Opportunity Finder ───────────────────────────────────────────

    def _find_csp_opportunity(
        self,
        symbol: str,
        usable_capital: float = 0.0,
    ) -> Optional[Signal]:
        """Find the best cash-secured put to sell on a given symbol.

        Filter chain:
          1. Get stock price
          2. Skip if earnings within buffer days
          3. Fetch put option chain in the DTE window
          4. For each candidate:
             - Estimate IV from mid price (Black-Scholes bisection)
             - Calculate real delta (Black-Scholes)
             - Filter: delta in range, IVR above minimum, OI sufficient, tight spread
             - Check affordability: strike * 100 <= allocation
          5. Score surviving candidates, pick the best
        """
        log.info("Scanning CSP for %s ...", symbol)

        # ── Step 1: Get stock price ───────────────────────────────────────
        quote = self.broker.get_stock_quote(symbol)
        if not quote:
            log.warning("CSP %s: could not get stock quote — skipping", symbol)
            return None
        stock_price = quote.price
        log.debug("CSP %s: stock price $%.2f", symbol, stock_price)

        # ── Step 2: Earnings check ────────────────────────────────────────
        # allow_on_failure=True for scan: other safety rails (stop loss,
        # circuit breaker) protect new entries; blocking all trades when
        # earnings data is unavailable is overly conservative here.
        if has_earnings_within(symbol, self.earnings_buffer_days, allow_on_failure=True):
            log.info("CSP %s: earnings within %d days — skipping",
                     symbol, self.earnings_buffer_days)
            return None

        # ── Step 3: Fetch put option chain ────────────────────────────────
        puts = self._fetch_option_contracts(symbol, option_type="put")
        if not puts:
            log.debug("CSP %s: no put contracts in DTE window", symbol)
            return None

        log.info("CSP %s: evaluating %d put contracts", symbol, len(puts))

        # ── Step 4: Filter and score candidates ───────────────────────────
        max_allocation = usable_capital * self.max_per_position_pct
        delta_lo = self.target_delta - 0.05
        delta_hi = self.target_delta + 0.10  # Slightly wider on the upside

        candidates: list[dict] = []
        for contract in puts:
            contract_dte = dte(contract.expiration_date)

            # DTE filter
            dte_ok, dte_msg = check_dte_range(contract_dte)
            if not dte_ok:
                continue

            # Skip zero-bid contracts
            if contract.bid <= 0:
                continue

            # Bid-ask spread filter
            spread = contract.ask - contract.bid
            if spread > self.max_bid_ask_spread:
                log.debug("CSP %s $%.2f: spread $%.2f > max $%.2f — skip",
                          symbol, contract.strike, spread, self.max_bid_ask_spread)
                continue

            # Open interest filter
            if int(contract.open_interest or 0) < self.min_open_interest:
                log.debug("CSP %s $%.2f: OI %d < min %d — skip",
                          symbol, contract.strike, contract.open_interest,
                          self.min_open_interest)
                continue

            # ── Estimate IV from mid price ────────────────────────────────
            mid_price = (contract.bid + contract.ask) / 2
            estimated_iv = _estimate_iv_from_price(
                stock_price, contract.strike, contract_dte, mid_price, option_type="put",
            )

            # ── Calculate real delta via Black-Scholes ────────────────────
            raw_delta = _estimate_delta(
                stock_price, contract.strike, contract_dte, estimated_iv, option_type="put",
            )
            abs_delta = abs(raw_delta)

            # Delta filter
            csp_ok, csp_msg = check_csp_delta(abs_delta)
            if not csp_ok:
                continue

            # IV percentage filter — we don't have historical IV data for
            # true IV Rank calculation.  Instead, we use IV percentile
            # thresholds as a proxy:
            #   - IV < 20% -> premiums too thin, skip
            #   - IV 20-30% -> marginal, lower score
            #   - IV > 30% -> sufficient premium to sell
            iv_pct = estimated_iv * 100  # Convert decimal to percentage (0.35 -> 35%)
            if iv_pct < 20:
                log.debug("CSP %s $%.2f: IV %.0f%% < 20%% — premiums too thin, skip",
                          symbol, contract.strike, iv_pct)
                continue

            # ── Affordability check ───────────────────────────────────────
            # Cash needed to secure the put = strike * 100
            required_capital = contract.strike * 100
            if required_capital > max_allocation:
                log.debug("CSP %s $%.2f: requires $%.0f > allocation $%.0f — skip",
                          symbol, contract.strike, required_capital, max_allocation)
                continue

            # ── Score using the Alpaca formula from rules.py ──────────────
            option_score = score_option(
                delta=abs_delta,
                dte=contract_dte,
                bid=contract.bid,
                strike=contract.strike,
            )

            candidates.append({
                "contract": contract,
                "delta": abs_delta,
                "raw_delta": raw_delta,
                "estimated_iv": estimated_iv,
                "iv_pct": iv_pct,
                "dte": contract_dte,
                "mid_price": mid_price,
                "score": option_score,
                "required_capital": required_capital,
            })

        if not candidates:
            log.info("CSP %s: no candidates passed all filters", symbol)
            return None

        # ── Step 5: Pick the best candidate ───────────────────────────────
        candidates.sort(key=lambda c: c["score"], reverse=True)
        best = candidates[0]
        contract = best["contract"]

        # Calculate pre-set exit targets
        entry_credit = best["mid_price"]
        target_close = entry_credit * (1.0 - self.profit_target_pct)
        stop_loss = entry_credit * self.stop_loss_multiplier

        log.info(
            "CSP %s SELECTED: $%.2f strike, %d DTE, delta %.2f, IV %.0f%%, "
            "bid $%.2f, score %.4f, capital $%.0f",
            symbol, contract.strike, best["dte"], best["delta"],
            best["estimated_iv"] * 100, contract.bid, best["score"],
            best["required_capital"],
        )

        return Signal(
            symbol=symbol,
            strategy=Strategy.WHEEL_CSP.value,
            action=SignalAction.SELL_CSP.value,
            option_type="put",
            strike=contract.strike,
            expiration_date=contract.expiration_date,
            limit_price=contract.bid,
            estimated_credit=contract.bid * 100,
            reason=(
                f"Wheel CSP on {symbol}: Sell ${contract.strike:.2f}P "
                f"({best['dte']} DTE, delta {best['delta']:.2f}, "
                f"IV {best['estimated_iv'] * 100:.0f}%)\n"
                f"Credit: ${contract.bid * 100:.0f} | "
                f"50% target: close at ${target_close:.2f} | "
                f"Stop: ${stop_loss:.2f}\n"
                f"Capital required: ${best['required_capital']:.0f} | "
                f"Score: {best['score']:.4f}"
            ),
            ai_analysis=(
                f"delta={best['delta']:.3f}, iv={best['estimated_iv']:.3f}, "
                f"iv_pct={best['iv_pct']:.1f}%, dte={best['dte']}, "
                f"oi={contract.open_interest}, spread=${contract.ask - contract.bid:.2f}"
            ),
            urgency=Urgency.NORMAL.value,
            optimal_execution_window="10:00-11:00 ET",
        )

    # ── Covered Call Opportunity Finder ───────────────────────────────────

    def _find_cc_opportunity(self, symbol: str) -> Optional[Signal]:
        """Find the best covered call to sell against held shares.

        CRITICAL SAFETY RULE: The call strike MUST be above the share cost
        basis.  Selling below cost basis locks in a guaranteed loss if called.

        Filter chain:
          1. Get stock price and shares position
          2. Fetch call option chain in the DTE window
          3. For each candidate:
             - Calculate delta (target ~0.30)
             - ENFORCE strike >= cost_basis
             - Filter OI and spread
          4. Score and pick best
        """
        log.info("Scanning CC for %s ...", symbol)

        # ── Step 1: Get share position from DB ────────────────────────────
        share_positions = self.db.get_open_positions(strategy=Strategy.WHEEL_SHARES.value)
        shares_pos = next(
            (p for p in share_positions if p.symbol == symbol), None,
        )
        if not shares_pos:
            log.warning("CC %s: no shares position found in DB — skipping", symbol)
            return None

        cost_basis = shares_pos.cost_basis or 0.0
        if cost_basis <= 0:
            log.warning("CC %s: cost basis is $%.2f (invalid) — skipping", symbol, cost_basis)
            return None

        log.info("CC %s: shares held, cost_basis=$%.2f", symbol, cost_basis)

        # ── Step 2: Get stock price ───────────────────────────────────────
        quote = self.broker.get_stock_quote(symbol)
        if not quote:
            log.warning("CC %s: could not get stock quote — skipping", symbol)
            return None
        stock_price = quote.price
        log.debug("CC %s: stock price $%.2f (cost_basis $%.2f)", symbol, stock_price, cost_basis)

        # ── Step 3: Fetch call option chain ───────────────────────────────
        calls = self._fetch_option_contracts(symbol, option_type="call")
        if not calls:
            log.debug("CC %s: no call contracts in DTE window", symbol)
            return None

        log.info("CC %s: evaluating %d call contracts", symbol, len(calls))

        # ── Step 4: Filter and score ──────────────────────────────────────
        cc_delta_lo = self.cc_delta - 0.10
        cc_delta_hi = self.cc_delta + 0.10

        candidates: list[dict] = []
        for contract in calls:
            contract_dte = dte(contract.expiration_date)

            # DTE filter
            dte_ok, _ = check_dte_range(contract_dte)
            if not dte_ok:
                continue

            if contract.bid <= 0:
                continue

            # Bid-ask spread filter
            spread = contract.ask - contract.bid
            if spread > self.max_bid_ask_spread:
                continue

            # Open interest filter
            if int(contract.open_interest or 0) < self.min_open_interest:
                continue

            # ── CRITICAL: Strike must be above cost basis ─────────────────
            cb_ok, cb_msg = check_cc_above_cost_basis(contract.strike, cost_basis)
            if not cb_ok:
                log.debug("CC %s $%.2f: %s", symbol, contract.strike, cb_msg)
                continue

            # ── Estimate IV and calculate delta ───────────────────────────
            mid_price = (contract.bid + contract.ask) / 2
            estimated_iv = _estimate_iv_from_price(
                stock_price, contract.strike, contract_dte, mid_price, option_type="call",
            )
            raw_delta = _estimate_delta(
                stock_price, contract.strike, contract_dte, estimated_iv, option_type="call",
            )
            abs_delta = abs(raw_delta)

            # Delta filter for CC (target ~0.30)
            if abs_delta < cc_delta_lo or abs_delta > cc_delta_hi:
                log.debug("CC %s $%.2f: delta %.2f outside [%.2f, %.2f] — skip",
                          symbol, contract.strike, abs_delta, cc_delta_lo, cc_delta_hi)
                continue

            option_score = score_option(
                delta=abs_delta,
                dte=contract_dte,
                bid=contract.bid,
                strike=contract.strike,
            )

            candidates.append({
                "contract": contract,
                "delta": abs_delta,
                "estimated_iv": estimated_iv,
                "dte": contract_dte,
                "mid_price": mid_price,
                "score": option_score,
            })

        if not candidates:
            log.info("CC %s: no candidates passed all filters (cost_basis=$%.2f)",
                     symbol, cost_basis)
            return None

        # ── Step 5: Pick the best ─────────────────────────────────────────
        candidates.sort(key=lambda c: c["score"], reverse=True)
        best = candidates[0]
        contract = best["contract"]

        # Potential upside if called away
        upside_per_share = contract.strike - cost_basis
        upside_total = upside_per_share * (shares_pos.quantity or 1) * 100

        log.info(
            "CC %s SELECTED: $%.2f strike, %d DTE, delta %.2f, IV %.0f%%, "
            "bid $%.2f, score %.4f | upside if called: $%.0f",
            symbol, contract.strike, best["dte"], best["delta"],
            best["estimated_iv"] * 100, contract.bid, best["score"],
            upside_total,
        )

        return Signal(
            symbol=symbol,
            strategy=Strategy.WHEEL_CC.value,
            action=SignalAction.SELL_CC.value,
            option_type="call",
            strike=contract.strike,
            expiration_date=contract.expiration_date,
            limit_price=contract.bid,
            estimated_credit=contract.bid * 100,
            reason=(
                f"Wheel CC on {symbol}: Sell ${contract.strike:.2f}C "
                f"({best['dte']} DTE, delta {best['delta']:.2f}, "
                f"IV {best['estimated_iv'] * 100:.0f}%)\n"
                f"Credit: ${contract.bid * 100:.0f} | "
                f"Cost basis: ${cost_basis:.2f} | "
                f"Strike above basis by ${contract.strike - cost_basis:.2f}\n"
                f"Upside if called: ${upside_total:.0f} + premium collected"
            ),
            ai_analysis=(
                f"delta={best['delta']:.3f}, iv={best['estimated_iv']:.3f}, "
                f"dte={best['dte']}, cost_basis={cost_basis:.2f}, "
                f"strike_margin={contract.strike - cost_basis:.2f}"
            ),
            urgency=Urgency.NORMAL.value,
            optimal_execution_window="10:00-11:00 ET",
        )

    # ── Exit Checking ────────────────────────────────────────────────────

    def check_exits(self, broker: object, positions: list[object]) -> list[Signal]:
        """Evaluate all open wheel positions for exit, roll, or close signals.

        CSP exit rules:
          1. Profit target hit (50% of credit)
          2. Stop loss breached (2x credit)
          3. DTE < roll threshold (7 days) — needs rolling
          4. Earnings within 3 days — emergency close

        CC exit rules:
          1. Profit target hit (50% of credit)
          2. DTE < roll threshold (7 days) — needs rolling
        """
        signals: list[Signal] = []

        for position in positions:
            strategy = position.strategy

            if strategy == Strategy.WHEEL_CSP.value:
                csp_signals = self._check_csp_exit(position)
                signals.extend(csp_signals)

            elif strategy == Strategy.WHEEL_CC.value:
                cc_signals = self._check_cc_exit(position)
                signals.extend(cc_signals)

            # WHEEL_SHARES positions don't have direct exit rules;
            # they exit via CC assignment or manual decision

        log.info("Wheel exit check: %d exit signals from %d positions",
                 len(signals), len(positions))
        return signals

    def _check_csp_exit(self, position: Position) -> list[Signal]:
        """Check a CSP position for all exit conditions."""
        signals: list[Signal] = []
        symbol = position.symbol

        # Need live pricing to make exit decisions
        if position.current_price is None:
            log.debug("CSP exit %s: no live price — skipping", symbol)
            return signals

        current_price = position.current_price
        entry_price = position.entry_price

        # ── Rule 1: Profit target ─────────────────────────────────────────
        profit_hit, profit_msg = check_profit_target(current_price, entry_price)
        if profit_hit:
            log.info("CSP %s: %s", symbol, profit_msg)
            pnl = (entry_price - current_price) * (position.quantity or 1) * 100
            signals.append(Signal(
                symbol=symbol,
                strategy=Strategy.WHEEL_CSP.value,
                action=SignalAction.BUY_TO_CLOSE.value,
                option_type="put",
                strike=position.strike,
                expiration_date=position.expiration_date,
                limit_price=current_price,
                estimated_pnl=pnl,
                reason=f"CSP profit target HIT: {profit_msg}",
                urgency=Urgency.NORMAL.value,
            ))
            return signals  # Profit target takes priority

        # ── Rule 2: Stop loss ─────────────────────────────────────────────
        stop_hit, stop_msg = check_stop_loss(current_price, entry_price)
        if stop_hit:
            log.warning("CSP %s: %s", symbol, stop_msg)
            loss = (current_price - entry_price) * (position.quantity or 1) * 100
            signals.append(Signal(
                symbol=symbol,
                strategy=Strategy.WHEEL_CSP.value,
                action=SignalAction.BUY_TO_CLOSE.value,
                option_type="put",
                strike=position.strike,
                expiration_date=position.expiration_date,
                limit_price=current_price,
                estimated_pnl=-loss,
                reason=f"CSP STOP LOSS: {stop_msg}",
                urgency=Urgency.URGENT.value,
                status=SignalStatus.AUTO_EXECUTED.value,  # Stop-loss must auto-execute
            ))
            return signals  # Stop loss is urgent

        # ── Rule 3: DTE roll check ────────────────────────────────────────
        current_dte = position.dte_remaining
        if current_dte is not None and current_dte < self.roll_dte_threshold:
            log.info("CSP %s: DTE %d < roll threshold %d — needs rolling",
                     symbol, current_dte, self.roll_dte_threshold)
            signals.append(Signal(
                symbol=symbol,
                strategy=Strategy.WHEEL_CSP.value,
                action=SignalAction.ROLL.value,
                option_type="put",
                strike=position.strike,
                expiration_date=position.expiration_date,
                limit_price=current_price,
                reason=(
                    f"CSP roll needed: {symbol} at {current_dte} DTE "
                    f"(threshold: {self.roll_dte_threshold}). "
                    f"Roll out to {self.target_dte_min}-{self.target_dte_max} DTE."
                ),
                urgency=Urgency.URGENT.value,
                status=SignalStatus.AUTO_EXECUTED.value,  # DTE < 7 = gamma risk, must auto-execute
            ))

        # ── Rule 4: Earnings proximity ────────────────────────────────────
        # allow_on_failure=False (default): conservative for exit engine —
        # when protecting an existing position, block on unknown earnings
        # to avoid 30-50% overnight gap risk.
        if has_earnings_within(symbol, 3, allow_on_failure=False):
            log.warning("CSP %s: EARNINGS within 3 days — emergency close", symbol)
            signals.append(Signal(
                symbol=symbol,
                strategy=Strategy.WHEEL_CSP.value,
                action=SignalAction.BUY_TO_CLOSE.value,
                option_type="put",
                strike=position.strike,
                expiration_date=position.expiration_date,
                limit_price=current_price,
                reason=f"EARNINGS ALERT: {symbol} reports within 3 days — closing CSP to avoid gap risk",
                urgency=Urgency.URGENT.value,
                status=SignalStatus.AUTO_EXECUTED.value,  # Earnings close must auto-execute
            ))

        return signals

    def _check_cc_exit(self, position: Position) -> list[Signal]:
        """Check a covered call position for exit conditions."""
        signals: list[Signal] = []
        symbol = position.symbol

        if position.current_price is None:
            log.debug("CC exit %s: no live price — skipping", symbol)
            return signals

        current_price = position.current_price
        entry_price = position.entry_price

        # ── Rule 1: Profit target ─────────────────────────────────────────
        profit_hit, profit_msg = check_profit_target(current_price, entry_price)
        if profit_hit:
            log.info("CC %s: %s", symbol, profit_msg)
            pnl = (entry_price - current_price) * (position.quantity or 1) * 100
            signals.append(Signal(
                symbol=symbol,
                strategy=Strategy.WHEEL_CC.value,
                action=SignalAction.BUY_TO_CLOSE.value,
                option_type="call",
                strike=position.strike,
                expiration_date=position.expiration_date,
                limit_price=current_price,
                estimated_pnl=pnl,
                reason=f"CC profit target HIT: {profit_msg}",
                urgency=Urgency.NORMAL.value,
            ))
            return signals

        # ── Rule 2: DTE roll check ────────────────────────────────────────
        current_dte = position.dte_remaining
        if current_dte is not None and current_dte < self.roll_dte_threshold:
            log.info("CC %s: DTE %d < roll threshold %d — needs rolling",
                     symbol, current_dte, self.roll_dte_threshold)
            signals.append(Signal(
                symbol=symbol,
                strategy=Strategy.WHEEL_CC.value,
                action=SignalAction.ROLL.value,
                option_type="call",
                strike=position.strike,
                expiration_date=position.expiration_date,
                limit_price=current_price,
                reason=(
                    f"CC roll needed: {symbol} at {current_dte} DTE "
                    f"(threshold: {self.roll_dte_threshold}). "
                    f"Roll out to {self.target_dte_min}-{self.target_dte_max} DTE, "
                    f"maintain strike above cost basis."
                ),
                urgency=Urgency.URGENT.value,
                status=SignalStatus.AUTO_EXECUTED.value,  # DTE < 7 = gamma risk, must auto-execute
            ))

        return signals

    # ── Assignment Handling ───────────────────────────────────────────────

    def handle_assignment(self, position: object) -> list[Signal]:
        """Handle option assignment — transition to the next wheel state.

        CSP assigned:
          - You now own 100 shares at the strike price
          - Cost basis = strike - premium collected from the CSP
          - Next step: sell a covered call above cost basis

        CC called away:
          - Shares sold at the CC strike price
          - Calculate total cycle P&L (all premiums + share appreciation)
          - Back to SCANNING state
        """
        signals: list[Signal] = []
        symbol = position.symbol

        if position.strategy == Strategy.WHEEL_CSP.value:
            signals.extend(self._handle_csp_assignment(position))

        elif position.strategy == Strategy.WHEEL_CC.value:
            signals.extend(self._handle_cc_assignment(position))

        else:
            log.warning("Assignment for unexpected strategy: %s %s",
                        symbol, position.strategy)

        return signals

    def _handle_csp_assignment(self, position: Position) -> list[Signal]:
        """CSP was assigned — create a shares position and prepare for CC.

        Cost basis calculation:
          cost_basis = strike_price - premium_collected_per_share
          (You bought at the strike but were paid premium to take that risk)
        """
        symbol = position.symbol
        strike = position.strike or 0.0
        premium_per_share = position.entry_price  # CSP entry credit per share
        cost_basis = strike - premium_per_share

        log.info(
            "CSP ASSIGNMENT: %s — bought shares at $%.2f strike, "
            "premium collected $%.2f/share, effective cost basis $%.2f",
            symbol, strike, premium_per_share, cost_basis,
        )

        # Mark the CSP position as assigned in DB
        if position.id:
            self.db.update_position(
                position.id,
                state="assigned",
                exit_reason="CSP assigned — shares acquired",
                exit_date=now_et().strftime("%Y-%m-%d"),
            )

        # Create a shares position in DB
        shares_position = Position(
            symbol=symbol,
            strategy=Strategy.WHEEL_SHARES.value,
            state="open",
            quantity=(position.quantity or 1) * 100,  # 1 option contract = 100 shares
            entry_date=now_et().strftime("%Y-%m-%d"),
            entry_price=strike,
            cost_basis=cost_basis,
            total_premium_collected=premium_per_share * 100,
            notes=f"Assigned from CSP at ${strike:.2f}, premium ${premium_per_share:.2f}",
        )
        shares_id = self.db.create_position(shares_position)
        log.info("Created shares position ID %d for %s (cost_basis=$%.2f)",
                 shares_id, symbol, cost_basis)

        # Signal to sell a covered call as the next step
        return [Signal(
            symbol=symbol,
            strategy=Strategy.WHEEL_SHARES.value,
            action=SignalAction.SELL_CC.value,
            reason=(
                f"CSP assigned on {symbol}. Now holding shares at "
                f"cost basis ${cost_basis:.2f}. Ready to sell covered call "
                f"with strike > ${cost_basis:.2f}."
            ),
            urgency=Urgency.NORMAL.value,
        )]

    def _handle_cc_assignment(self, position: Position) -> list[Signal]:
        """CC was called away — shares sold at strike, calculate full cycle P&L.

        Total cycle P&L =
          share_appreciation (cc_strike - cost_basis) * 100
          + total_premium_collected (CSP credit + CC credit + any prior CCs)
        """
        symbol = position.symbol
        cc_strike = position.strike or 0.0
        cc_premium = position.entry_price  # CC entry credit per share

        # Find the related shares position to get cost basis
        share_positions = self.db.get_open_positions(strategy=Strategy.WHEEL_SHARES.value)
        shares_pos = next(
            (p for p in share_positions if p.symbol == symbol), None,
        )

        cost_basis = shares_pos.cost_basis if shares_pos else 0.0
        prior_premium = shares_pos.total_premium_collected if shares_pos else 0.0

        # Calculate P&L
        share_appreciation = (cc_strike - cost_basis) * 100
        total_premium = prior_premium + (cc_premium * 100)
        total_pnl = share_appreciation + total_premium

        log.info(
            "CC ASSIGNMENT: %s — shares called away at $%.2f\n"
            "  Cost basis: $%.2f | Share appreciation: $%.0f\n"
            "  Total premium collected: $%.0f\n"
            "  TOTAL CYCLE P&L: $%.0f",
            symbol, cc_strike, cost_basis,
            share_appreciation, total_premium, total_pnl,
        )

        # Close the CC position
        if position.id:
            self.db.update_position(
                position.id,
                state="assigned",
                exit_reason="CC assigned — shares called away",
                exit_date=now_et().strftime("%Y-%m-%d"),
                pnl_dollars=cc_premium * 100,
            )

        # Close the shares position
        if shares_pos and shares_pos.id:
            self.db.update_position(
                shares_pos.id,
                state="closed",
                exit_reason=f"Called away at ${cc_strike:.2f}",
                exit_date=now_et().strftime("%Y-%m-%d"),
                exit_price=cc_strike,
                pnl_dollars=total_pnl,
                total_premium_collected=total_premium,
            )

        return [Signal(
            symbol=symbol,
            strategy=Strategy.WHEEL_CC.value,
            action=SignalAction.BUY_TO_CLOSE.value,
            estimated_pnl=total_pnl,
            reason=(
                f"Wheel cycle COMPLETE for {symbol}! "
                f"Shares called away at ${cc_strike:.2f}.\n"
                f"Share gain: ${share_appreciation:.0f} | "
                f"Premium collected: ${total_premium:.0f} | "
                f"Total P&L: ${total_pnl:.0f}\n"
                f"Symbol returns to SCANNING state."
            ),
            urgency=Urgency.INFO.value,
        )]

    # ── Option Chain Fetcher ─────────────────────────────────────────────

    def _fetch_option_contracts(
        self,
        symbol: str,
        option_type: str = "put",
    ) -> list[BrokerContract]:
        """Fetch option contracts from Alpaca within the target DTE window.

        Uses direct Alpaca SDK calls (same pattern as vrp_spreads.py) to get
        contracts and their live quotes.  Returns broker OptionContract objects
        with bid/ask/mark/open_interest populated.
        """
        today = now_et().date()
        min_exp = (today + timedelta(days=self.target_dte_min)).isoformat()
        max_exp = (today + timedelta(days=self.target_dte_max)).isoformat()

        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status="active",
            expiration_date_gte=min_exp,
            expiration_date_lte=max_exp,
            type=option_type,
        )

        try:
            result = self.broker.trading.get_option_contracts(req)
            contracts = result.option_contracts if result else []
        except Exception as exc:
            log.warning("Failed to fetch %s %s options: %s", symbol, option_type, exc)
            return []

        if not contracts:
            return []

        # Fetch live quotes for all contracts (limit to 50 per Alpaca batch)
        contract_symbols = [c.symbol for c in contracts[:50]]
        try:
            quote_req = OptionLatestQuoteRequest(symbol_or_symbols=contract_symbols)
            quotes = self.broker.option_data.get_option_latest_quote(quote_req)
        except Exception as exc:
            log.warning("Failed to get option quotes for %s: %s", symbol, exc)
            quotes = {}

        results: list[BrokerContract] = []
        for contract in contracts[:50]:
            q = quotes.get(contract.symbol)
            if not q or not q.bid_price or float(q.bid_price) <= 0:
                continue

            bid = float(q.bid_price)
            ask = float(q.ask_price) if q.ask_price else 0.0
            exp_str = (
                contract.expiration_date.isoformat()
                if contract.expiration_date
                else ""
            )

            results.append(BrokerContract(
                symbol=contract.symbol,
                option_type=option_type,
                strike=float(contract.strike_price),
                expiration_date=exp_str,
                bid=bid,
                ask=ask,
                mark=(bid + ask) / 2 if (bid + ask) > 0 else 0.0,
                delta=None,  # Not provided by Alpaca — we calculate via BS
                open_interest=getattr(contract, "open_interest", 0) or 0,
            ))

        log.debug("Fetched %d %s %s contracts with live quotes",
                   len(results), symbol, option_type)
        return results

    # ── Helpers ───────────────────────────────────────────────────────────

    def _count_active_wheel_symbols(self, states: dict[str, WheelState]) -> int:
        """Count how many distinct symbols are actively in the wheel.

        Only counts symbols with an open CSP, held shares, or open CC.
        Symbols in SCANNING state are not counted.
        """
        return sum(
            1 for state in states.values()
            if state != WheelState.SCANNING
        )
