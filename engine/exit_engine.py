"""ExitEngine — monitors open positions and triggers exit/roll signals."""

from __future__ import annotations

from typing import TYPE_CHECKING

from data import database as db
from data.models import (
    Position,
    Signal,
    SignalAction,
    SignalStatus,
    Strategy,
    Urgency,
)
from strategies.pmcc import PMCCStrategy  # kept for legacy PMCC position exits
from utils.config import get
from utils.logger import get_logger
from utils.market import dte
from utils.timing import now_et

if TYPE_CHECKING:
    from broker.alpaca_broker import AlpacaBroker
    from engine.executor import OrderExecutor
    from engine.signal import SignalQueue

log = get_logger(__name__)


class ExitEngine:
    """Scans open positions for exit conditions and routes resulting signals.

    Auto-execute signals (profit targets) are sent straight to the executor.
    Approval-needed signals are placed in the signal queue for Discord
    approve/deny flow.
    """

    def __init__(
        self,
        broker: AlpacaBroker,
        signal_queue: SignalQueue,
        executor: OrderExecutor,
    ) -> None:
        self.broker = broker
        self.signal_queue = signal_queue
        self.executor = executor

        # Wheel exit thresholds from config
        self.wheel_profit_target: float = get("wheel.profit_target_pct", 0.50)
        self.wheel_stop_multiplier: float = get("wheel.stop_loss_multiplier", 2.0)
        self.wheel_roll_dte: int = get("wheel.roll_dte_threshold", 7)

    # ── Public API ────────────────────────────────────────────────────────

    def check_all_positions(self) -> list[Signal]:
        """Run exit checks on every open position.

        Returns:
            All signals generated (both auto-executed and queued).
        """
        positions = db.get_open_positions()
        if not positions:
            log.debug("No open positions to check")
            return []

        log.info("Checking %d open position(s) for exit conditions", len(positions))

        all_signals: list[Signal] = []

        # Separate positions by strategy family
        pmcc_positions: list[Position] = []
        wheel_positions: list[Position] = []

        for pos in positions:
            self._refresh_position(pos)

            if pos.strategy in (Strategy.PMCC_LEAPS.value, Strategy.PMCC_SHORT_CALL.value):
                pmcc_positions.append(pos)
            elif pos.strategy in (
                Strategy.WHEEL_CSP.value,
                Strategy.WHEEL_CC.value,
                Strategy.WHEEL_SHARES.value,
            ):
                wheel_positions.append(pos)

        # PMCC exit checks — delegate to PMCCStrategy (legacy positions)
        if pmcc_positions:
            pmcc_strategy = PMCCStrategy(self.broker, db)
            pmcc_signals = pmcc_strategy.check_positions(pmcc_positions)
            all_signals.extend(pmcc_signals)

        # Wheel exit checks — inline
        for pos in wheel_positions:
            wheel_signals = self._check_wheel_exits(pos)
            all_signals.extend(wheel_signals)

        # Route signals
        for sig in all_signals:
            if sig.status == SignalStatus.AUTO_EXECUTED.value:
                self._auto_execute(sig, positions)
            else:
                sig.id = self.signal_queue.create(sig)
                log.info(
                    "Signal #%d queued for approval: %s %s (%s)",
                    sig.id or 0, sig.action, sig.symbol, sig.reason,
                )

        log.info(
            "Exit check complete: %d signal(s) generated (%d auto, %d queued)",
            len(all_signals),
            sum(1 for s in all_signals if s.status == SignalStatus.AUTO_EXECUTED.value),
            sum(1 for s in all_signals if s.status != SignalStatus.AUTO_EXECUTED.value),
        )
        return all_signals

    def has_near_stop_positions(self) -> bool:
        """Check whether any open position is near its stop-loss level.

        Used by the bot to decide whether to run the fast (1-minute) monitoring loop.
        A position is 'near stop' if its current P&L loss is within 50% of its stop-loss level.
        """
        positions = db.get_open_positions()
        for pos in positions:
            if self._is_near_stop(pos):
                return True
        return False

    def _is_near_stop(self, pos: Position) -> bool:
        """Return True if *pos* is within 50% of its stop-loss level.

        For short options (CSP, CC, VRP short leg), stop-loss triggers when
        current_price >= stop_loss_price.  'Near stop' means
        current_price >= midpoint between entry and stop.
        """
        if (
            pos.current_price is None
            or pos.entry_price <= 0
            or pos.stop_loss_price is None
            or pos.stop_loss_price <= 0
        ):
            return False

        # Distance from entry to stop
        stop_distance = pos.stop_loss_price - pos.entry_price
        if stop_distance <= 0:
            return False

        # Current distance moved toward stop
        current_distance = pos.current_price - pos.entry_price

        # Near stop = moved at least 50% of the way to the stop
        return current_distance >= stop_distance * 0.50

    # ── Position refresh ──────────────────────────────────────────────────

    def _refresh_position(self, pos: Position) -> None:
        """Fetch live market data and update the position in the database."""
        if not pos.id:
            return

        updates: dict = {}

        # Fetch current option price — try direct quote first (faster than full chain)
        if pos.option_type and pos.strike and pos.expiration_date:
            try:
                from alpaca.data.requests import OptionLatestQuoteRequest

                # Build OCC symbol to fetch a direct quote
                occ_symbol = self.broker._build_option_symbol(
                    pos.symbol, pos.strike, pos.expiration_date,
                    pos.option_type,
                ) if hasattr(self.broker, "_build_option_symbol") else None

                if occ_symbol:
                    req = OptionLatestQuoteRequest(symbol_or_symbols=[occ_symbol])
                    quotes = self.broker.option_data.get_option_latest_quote(req)
                    q = quotes.get(occ_symbol)
                    if q and q.bid_price:
                        bid = float(q.bid_price)
                        ask = float(q.ask_price) if q.ask_price else bid
                        updates["current_price"] = (bid + ask) / 2
                        pos.current_price = updates["current_price"]
                else:
                    # Fallback to full chain if we can't build OCC symbol
                    chain = self.broker.get_option_chain(
                        pos.symbol, expiration_date=pos.expiration_date,
                    )
                    match = self._find_contract(chain, pos)
                    if match:
                        updates["current_price"] = match.mark or ((match.bid + match.ask) / 2)
                        pos.current_price = updates["current_price"]
            except Exception as exc:
                log.warning("Failed to refresh price for position #%d: %s", pos.id, exc)

        # Recalculate DTE
        if pos.expiration_date:
            try:
                remaining = dte(pos.expiration_date)
                updates["dte_remaining"] = remaining
                pos.dte_remaining = remaining
            except (ValueError, TypeError):
                pass

        # Recalculate P&L
        if pos.current_price is not None and pos.entry_price:
            pnl_dollars = (pos.entry_price - pos.current_price) * pos.quantity * 100
            pnl_percent = (pnl_dollars / (pos.entry_price * pos.quantity * 100)) * 100
            updates["pnl_dollars"] = round(pnl_dollars, 2)
            updates["pnl_percent"] = round(pnl_percent, 2)
            pos.pnl_dollars = updates["pnl_dollars"]
            pos.pnl_percent = updates["pnl_percent"]

        if updates:
            db.update_position(pos.id, **updates)
            log.debug(
                "Position #%d refreshed: price=$%s, delta=%s, dte=%s",
                pos.id,
                updates.get("current_price"),
                updates.get("current_delta"),
                updates.get("dte_remaining"),
            )

    def _find_contract(self, chain: list, pos: Position):
        """Find the matching contract in an option chain for a position."""
        for contract in chain:
            if (
                contract.option_type == pos.option_type
                and abs(contract.strike - (pos.strike or 0)) < 0.01
            ):
                return contract
        return None

    # ── Wheel exit logic ──────────────────────────────────────────────────

    def _check_wheel_exits(self, pos: Position) -> list[Signal]:
        """Check Wheel (CSP / CC) position for exit conditions."""
        signals: list[Signal] = []

        if pos.current_price is None or pos.entry_price <= 0:
            return signals

        # 1. Profit target — 50% of credit captured (auto-execute)
        target_price = pos.entry_price * (1 - self.wheel_profit_target)
        if pos.current_price <= target_price:
            signals.append(Signal(
                symbol=pos.symbol,
                strategy=pos.strategy,
                action=SignalAction.BUY_TO_CLOSE.value,
                option_type=pos.option_type,
                strike=pos.strike,
                expiration_date=pos.expiration_date,
                limit_price=pos.current_price,
                estimated_pnl=(pos.entry_price - pos.current_price) * pos.quantity * 100,
                reason=(
                    f"Wheel {self.wheel_profit_target:.0%} profit target hit. "
                    f"Entry: ${pos.entry_price:.2f}, Current: ${pos.current_price:.2f}"
                ),
                urgency=Urgency.NORMAL.value,
                status=SignalStatus.AUTO_EXECUTED.value,
            ))
            return signals  # Don't stack exit signals

        # 2. Stop loss — current price >= 2x entry credit
        stop_price = pos.entry_price * self.wheel_stop_multiplier
        if pos.current_price >= stop_price:
            signals.append(Signal(
                symbol=pos.symbol,
                strategy=pos.strategy,
                action=SignalAction.BUY_TO_CLOSE.value,
                option_type=pos.option_type,
                strike=pos.strike,
                expiration_date=pos.expiration_date,
                limit_price=pos.current_price,
                estimated_pnl=(pos.entry_price - pos.current_price) * pos.quantity * 100,
                reason=(
                    f"STOP LOSS: current ${pos.current_price:.2f} >= "
                    f"{self.wheel_stop_multiplier}x entry ${stop_price:.2f}"
                ),
                urgency=Urgency.URGENT.value,
                status=SignalStatus.AUTO_EXECUTED.value,  # MUST auto-execute, not wait for approval
            ))
            return signals

        # 3. DTE roll — fewer than 7 days remaining
        if pos.dte_remaining is not None and pos.dte_remaining < self.wheel_roll_dte:
            signals.append(Signal(
                symbol=pos.symbol,
                strategy=pos.strategy,
                action=SignalAction.ROLL.value,
                option_type=pos.option_type,
                strike=pos.strike,
                expiration_date=pos.expiration_date,
                limit_price=pos.current_price,
                reason=(
                    f"DTE={pos.dte_remaining} < threshold={self.wheel_roll_dte}. "
                    f"Time to roll forward."
                ),
                urgency=Urgency.URGENT.value,
                status=SignalStatus.AUTO_EXECUTED.value,  # DTE < 7 = gamma risk, must auto-execute
            ))

        return signals

    # ── Auto-execution ────────────────────────────────────────────────────

    def _auto_execute(self, signal: Signal, positions: list[Position]) -> None:
        """Find the matching position and execute the exit immediately."""
        target_pos = self._match_position(signal, positions)
        if not target_pos:
            log.warning(
                "Auto-execute: no matching position for %s %s $%s — queueing instead",
                signal.action, signal.symbol, signal.strike,
            )
            signal.status = SignalStatus.PENDING.value
            signal.id = self.signal_queue.create(signal)
            return

        # NEW-C1/NEW-I2: Skip if position is already closed
        if target_pos.state == 'closed':
            log.info("Position #%d already closed — skipping", target_pos.id)
            return

        # NEW-C1/NEW-I2: Skip if position already has a pending close execution
        pending = db.get_pending_executions()
        for exe in pending:
            if exe.position_id == target_pos.id:
                log.info("Position #%d already has pending close execution #%d — skipping duplicate",
                         target_pos.id, exe.id)
                return  # Don't place another close order

        try:
            execution = self.executor.execute_auto_exit(signal, target_pos)
            if signal.id:
                self.signal_queue.mark_auto_executed(signal.id)
            log.info(
                "Auto-executed %s %s position #%d — exec #%s",
                signal.action, signal.symbol, target_pos.id or 0, execution.id,
            )
        except Exception as exc:
            log.error(
                "Auto-execute failed for %s %s: %s — queueing for manual review",
                signal.action, signal.symbol, exc,
            )
            signal.status = SignalStatus.PENDING.value
            signal.id = self.signal_queue.create(signal)

    def _match_position(self, signal: Signal, positions: list[Position]) -> Position | None:
        """Find the open position that a signal refers to."""
        for pos in positions:
            if (
                pos.symbol == signal.symbol
                and pos.option_type == signal.option_type
                and pos.strike == signal.strike
                and pos.expiration_date == signal.expiration_date
            ):
                return pos
        return None
