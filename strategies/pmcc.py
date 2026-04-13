"""Poor Man's Covered Call (PMCC) strategy implementation.

PMCC structure:
  - Long leg: Buy deep ITM LEAPS call (delta 0.75-0.80, 6+ months DTE)
  - Short leg: Sell OTM call against LEAPS (delta 0.20-0.30, 10-45 DTE)

The strategy generates income by selling short-dated calls while owning
a long-dated call as a substitute for 100 shares. Much less capital required
than a traditional covered call.
"""

from __future__ import annotations

import uuid
from typing import Optional

from data.models import Position, Signal, SignalAction, Strategy, Urgency
from utils.config import get
from utils.logger import get_logger
from utils.market import dte, has_earnings_within
from utils.timing import format_et, in_optimal_entry_window, in_roll_window, now_et

log = get_logger(__name__)


class PMCCStrategy:
    """PMCC strategy manager — handles LEAPS selection, short call management, rolling."""

    def __init__(self, broker, db):
        self.broker = broker
        self.db = db

        # Load config
        self.leaps_delta = get("pmcc.leaps_delta", 0.80)
        self.leaps_min_dte = get("pmcc.leaps_min_dte", 180)
        self.short_delta = get("pmcc.short_call_delta", 0.25)
        self.short_dte_min = get("pmcc.short_call_dte_min", 10)
        self.short_dte_max = get("pmcc.short_call_dte_max", 45)
        self.profit_target = get("pmcc.profit_target_pct", 0.50)
        self.roll_dte = get("pmcc.roll_dte_threshold", 7)
        self.roll_extrinsic = get("pmcc.roll_extrinsic_threshold", 0.10)
        self.assignment_floor = get("pmcc.assignment_extrinsic_floor", 0.05)
        self.dividend_buffer = get("pmcc.dividend_buffer_days", 7)
        self.leaps_min_before_roll = get("pmcc.leaps_min_dte_before_roll", 90)
        self.max_loss_alert = get("pmcc.max_loss_alert_pct", 0.30)
        self.compound_alert = get("pmcc.profit_compound_alert_pct", 0.50)

    # ── LEAPS Selection ────────────────────────────────────────────────────

    def find_leaps_candidates(self, symbol: str) -> list[dict]:
        """Find suitable LEAPS calls for the given symbol.

        Returns a list of candidates sorted by score, each with:
        strike, expiration, bid, ask, delta, dte, score
        """
        log.info("Scanning LEAPS candidates for %s", symbol)
        candidates = []

        chain = self.broker.get_option_chain(symbol)
        if not chain:
            log.warning("No option chain available for %s", symbol)
            return []

        for contract in chain:
            if contract.option_type != "call":
                continue

            contract_dte = dte(contract.expiration_date)

            # Must be 6+ months out
            if contract_dte < self.leaps_min_dte:
                continue

            # Must be deep ITM (high delta)
            if contract.delta is None:
                continue
            if not (0.70 <= contract.delta <= 0.90):
                continue

            # Must have reasonable liquidity
            if contract.open_interest < 100:
                continue
            if contract.ask - contract.bid > 1.00:
                continue

            # Score: prefer delta closest to target, longer DTE, tighter spread
            delta_score = 1 - abs(contract.delta - self.leaps_delta)
            dte_score = min(contract_dte / 365, 1.0)  # Normalize to 1yr
            spread_score = 1 - min((contract.ask - contract.bid) / contract.ask, 1.0)
            score = delta_score * 0.5 + dte_score * 0.3 + spread_score * 0.2

            candidates.append({
                "symbol": symbol,
                "strike": contract.strike,
                "expiration": contract.expiration_date,
                "bid": contract.bid,
                "ask": contract.ask,
                "mark": (contract.bid + contract.ask) / 2,
                "delta": contract.delta,
                "theta": contract.theta,
                "iv": contract.iv,
                "dte": contract_dte,
                "cost": contract.ask * 100,  # Per-contract cost
                "open_interest": contract.open_interest,
                "score": round(score, 4),
            })

        candidates.sort(key=lambda c: c["score"], reverse=True)
        log.info("Found %d LEAPS candidates for %s", len(candidates), symbol)
        return candidates[:5]  # Top 5

    def generate_leaps_signal(self, candidate: dict) -> Signal:
        """Create a signal to buy a LEAPS call."""
        return Signal(
            symbol=candidate["symbol"],
            strategy=Strategy.PMCC_LEAPS.value,
            action=SignalAction.BUY_LEAPS.value,
            option_type="call",
            strike=candidate["strike"],
            expiration_date=candidate["expiration"],
            limit_price=candidate["ask"],
            estimated_credit=-candidate["cost"],  # Negative because it's a debit
            reason=(
                f"LEAPS candidate: ${candidate['strike']} call, "
                f"{candidate['dte']} DTE, delta {candidate['delta']:.2f}, "
                f"cost ${candidate['cost']:.0f}"
            ),
            urgency=Urgency.NORMAL.value,
            optimal_execution_window="10:00-11:00 ET",
        )

    # ── Short Call Selection ───────────────────────────────────────────────

    def find_short_call_candidates(self, leaps_position: Position) -> list[dict]:
        """Find suitable short calls to sell against an existing LEAPS position.

        The short call strike MUST be above the LEAPS break-even
        (LEAPS strike + premium paid per share).
        """
        symbol = leaps_position.symbol
        leaps_breakeven = leaps_position.strike + leaps_position.entry_price
        log.info(
            "Scanning short calls for %s (LEAPS break-even: $%.2f)",
            symbol, leaps_breakeven,
        )

        candidates = []
        chain = self.broker.get_option_chain(symbol)
        if not chain:
            return []

        for contract in chain:
            if contract.option_type != "call":
                continue

            contract_dte = dte(contract.expiration_date)

            # DTE window: 10-45 days
            if not (self.short_dte_min <= contract_dte <= self.short_dte_max):
                continue

            # Delta range
            if contract.delta is None:
                continue
            if not (0.15 <= contract.delta <= 0.35):
                continue

            # Strike MUST be above LEAPS break-even (safety rule)
            if contract.strike <= leaps_breakeven:
                continue

            # Liquidity
            if contract.open_interest < 200:
                continue
            if contract.ask - contract.bid > 0.15:
                continue

            # Score using Alpaca formula: (1-|Δ|) × (250/(DTE+5)) × (bid/strike)
            score = (1 - abs(contract.delta)) * (250 / (contract_dte + 5)) * (contract.bid / contract.strike)

            candidates.append({
                "symbol": symbol,
                "strike": contract.strike,
                "expiration": contract.expiration_date,
                "bid": contract.bid,
                "ask": contract.ask,
                "mark": (contract.bid + contract.ask) / 2,
                "delta": contract.delta,
                "theta": contract.theta,
                "iv": contract.iv,
                "dte": contract_dte,
                "credit": contract.bid * 100,
                "open_interest": contract.open_interest,
                "safety_margin": contract.strike - leaps_breakeven,
                "score": round(score, 6),
            })

        candidates.sort(key=lambda c: c["score"], reverse=True)
        log.info("Found %d short call candidates for %s", len(candidates), symbol)
        return candidates[:5]

    def generate_short_call_signal(
        self, candidate: dict, leaps_position: Position
    ) -> Signal:
        """Create a signal to sell a short call against LEAPS."""
        leaps_breakeven = leaps_position.strike + leaps_position.entry_price
        pair_id = leaps_position.pair_id

        return Signal(
            symbol=candidate["symbol"],
            strategy=Strategy.PMCC_SHORT_CALL.value,
            action=SignalAction.SELL_SHORT_CALL.value,
            option_type="call",
            strike=candidate["strike"],
            expiration_date=candidate["expiration"],
            limit_price=candidate["bid"],
            estimated_credit=candidate["credit"],
            reason=(
                f"Short call: ${candidate['strike']} call, "
                f"{candidate['dte']} DTE, delta {candidate['delta']:.2f}, "
                f"credit ${candidate['credit']:.0f}, "
                f"safety margin ${candidate['safety_margin']:.2f} above break-even"
            ),
            urgency=Urgency.NORMAL.value,
            optimal_execution_window="10:00-11:00 ET",
        )

    # ── Position Health Checks ─────────────────────────────────────────────

    def check_positions(self, positions: list[Position]) -> list[Signal]:
        """Run all PMCC health checks on open positions. Returns exit/roll signals."""
        signals = []

        for pos in positions:
            if pos.strategy == Strategy.PMCC_SHORT_CALL.value:
                signals.extend(self._check_short_call(pos))
            elif pos.strategy == Strategy.PMCC_LEAPS.value:
                signals.extend(self._check_leaps(pos))

        return signals

    def _check_short_call(self, pos: Position) -> list[Signal]:
        """Check a short call for exit, roll, or assignment risk."""
        signals = []
        if pos.current_price is None or pos.dte_remaining is None:
            return signals

        # 1. Profit target — auto-execute
        if pos.current_price <= pos.entry_price * (1 - self.profit_target):
            signals.append(Signal(
                symbol=pos.symbol,
                strategy=pos.strategy,
                action=SignalAction.BUY_TO_CLOSE.value,
                option_type="call",
                strike=pos.strike,
                expiration_date=pos.expiration_date,
                limit_price=pos.current_price,
                estimated_pnl=(pos.entry_price - pos.current_price) * pos.quantity * 100,
                reason=f"50% profit target hit. Entry: ${pos.entry_price:.2f}, Current: ${pos.current_price:.2f}",
                urgency=Urgency.NORMAL.value,
                status="auto_executed",  # Auto-execute exits
            ))
            return signals  # Don't check other rules if we're closing

        # 2. Roll needed — DTE < threshold
        if pos.dte_remaining < self.roll_dte:
            signals.append(Signal(
                symbol=pos.symbol,
                strategy=pos.strategy,
                action=SignalAction.ROLL.value,
                option_type="call",
                strike=pos.strike,
                expiration_date=pos.expiration_date,
                limit_price=pos.current_price,
                reason=f"DTE={pos.dte_remaining} < threshold={self.roll_dte}. Time to roll.",
                urgency=Urgency.URGENT.value,
            ))

        # 3. Roll needed — extrinsic value too low
        # Extrinsic = option price - intrinsic value
        # For now approximate: if option is cheap enough, roll
        if pos.current_price < self.roll_extrinsic:
            signals.append(Signal(
                symbol=pos.symbol,
                strategy=pos.strategy,
                action=SignalAction.ROLL.value,
                option_type="call",
                strike=pos.strike,
                expiration_date=pos.expiration_date,
                limit_price=pos.current_price,
                reason=f"Extrinsic value ${pos.current_price:.2f} < ${self.roll_extrinsic}. Roll for more premium.",
                urgency=Urgency.URGENT.value,
            ))

        # 4. Assignment risk — extrinsic near zero AND ITM
        if pos.current_delta and pos.current_delta > 0.50:  # ITM
            if pos.current_price < self.assignment_floor:
                signals.append(Signal(
                    symbol=pos.symbol,
                    strategy=pos.strategy,
                    action=SignalAction.BUY_TO_CLOSE.value,
                    option_type="call",
                    strike=pos.strike,
                    expiration_date=pos.expiration_date,
                    limit_price=pos.current_price,
                    reason=f"ASSIGNMENT RISK: ITM (delta {pos.current_delta:.2f}) with extrinsic < ${self.assignment_floor}. Close immediately!",
                    urgency=Urgency.URGENT.value,
                ))

        return signals

    def _check_leaps(self, pos: Position) -> list[Signal]:
        """Check LEAPS health — DTE, value decline, profit compounding."""
        signals = []
        if pos.dte_remaining is None:
            return signals

        # 1. LEAPS DTE getting low — time to roll to new LEAPS
        if pos.dte_remaining < self.leaps_min_before_roll:
            signals.append(Signal(
                symbol=pos.symbol,
                strategy=pos.strategy,
                action=SignalAction.ROLL.value,
                option_type="call",
                strike=pos.strike,
                expiration_date=pos.expiration_date,
                reason=f"LEAPS DTE={pos.dte_remaining} < {self.leaps_min_before_roll}. Time decay accelerating — roll to new LEAPS.",
                urgency=Urgency.URGENT.value,
            ))

        # 2. Max loss alert — LEAPS dropped significantly
        if pos.current_price is not None and pos.entry_price > 0:
            loss_pct = (pos.entry_price - pos.current_price) / pos.entry_price
            if loss_pct >= self.max_loss_alert:
                signals.append(Signal(
                    symbol=pos.symbol,
                    strategy=pos.strategy,
                    action=SignalAction.CLOSE_PAIR.value,
                    reason=f"LEAPS value dropped {loss_pct:.0%} from entry. Manual review recommended.",
                    urgency=Urgency.URGENT.value,
                ))

        # 3. Profit compounding milestone
        if pos.total_premium_collected > 0 and pos.entry_price > 0:
            cost = pos.entry_price * pos.quantity * 100
            if pos.total_premium_collected >= cost * self.compound_alert:
                pct = pos.total_premium_collected / cost
                signals.append(Signal(
                    symbol=pos.symbol,
                    strategy=pos.strategy,
                    action=SignalAction.CLOSE_PAIR.value,  # Info only
                    reason=f"Milestone: collected ${pos.total_premium_collected:.0f} in premium ({pct:.0%} of LEAPS cost ${cost:.0f}). Cost basis very favorable!",
                    urgency=Urgency.INFO.value,
                ))

        return signals

    # ── Entry Scanning ─────────────────────────────────────────────────────

    def scan_for_entries(self, universe: list[str]) -> list[Signal]:
        """Scan for new PMCC opportunities — either new LEAPS or new short calls."""
        signals = []

        # Check if we have any open LEAPS without a short call
        open_leaps = self.db.get_open_positions(strategy=Strategy.PMCC_LEAPS.value)
        open_shorts = self.db.get_open_positions(strategy=Strategy.PMCC_SHORT_CALL.value)

        active_short_pairs = {p.pair_id for p in open_shorts if p.pair_id}

        for leaps in open_leaps:
            # If this LEAPS has no active short call, find one
            if leaps.pair_id and leaps.pair_id not in active_short_pairs:
                if has_earnings_within(leaps.symbol, 14):
                    log.info("Skipping short call for %s — earnings within 14 days", leaps.symbol)
                    continue

                candidates = self.find_short_call_candidates(leaps)
                if candidates:
                    sig = self.generate_short_call_signal(candidates[0], leaps)
                    signals.append(sig)
                    log.info("Generated short call signal for %s: $%s call", leaps.symbol, candidates[0]["strike"])

        # If no open LEAPS, scan for new ones
        if not open_leaps:
            for symbol in universe:
                if has_earnings_within(symbol, 14):
                    continue

                candidates = self.find_leaps_candidates(symbol)
                if candidates:
                    sig = self.generate_leaps_signal(candidates[0])
                    signals.append(sig)
                    log.info("Generated LEAPS signal for %s: $%s call", symbol, candidates[0]["strike"])
                    break  # Only suggest one LEAPS at a time

        return signals
