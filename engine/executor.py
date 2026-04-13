"""OrderExecutor — places orders on Alpaca with optimal timing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from data import database as db
from data.models import (
    Execution,
    OrderStatus,
    Position,
    Signal,
    SignalAction,
    SignalStatus,
    Strategy,
    Urgency,
)
from engine.circuit_breaker import CircuitBreaker
from utils.config import get
from utils.logger import get_logger
from utils.timing import format_et, now_et

if TYPE_CHECKING:
    from broker.alpaca_broker import AlpacaBroker

log = get_logger(__name__)


class OrderExecutor:
    """Places orders on Alpaca and records executions in the database.

    When ``broker.paper_trade`` is enabled in config, no real orders are sent.
    The executor still creates DB records so the rest of the pipeline
    (order tracking, exit engine, performance) works identically.
    """

    def __init__(self, broker: AlpacaBroker, webhook: object | None = None) -> None:
        self.broker = broker
        self.paper_trade: bool = get("broker.paper_trade", False)
        self.circuit_breaker = CircuitBreaker(webhook=webhook)

    # ── Public API ────────────────────────────────────────────────────────

    def execute_signal(self, signal: Signal) -> Execution:
        """Place an order for *signal* and return the resulting Execution.

        Steps:
          1. Check for duplicate orders (same signal or same position).
          2. Check buying power is sufficient.
          3. Map signal.action to the correct broker call.
          4. Place the order (or simulate in paper mode).
          5. Create an Execution record in the database.
        """
        log.info(
            "Executing signal: %s %s %s $%s %s (limit $%s)",
            signal.action, signal.symbol, signal.option_type or "",
            signal.strike or "", signal.expiration_date or "",
            signal.limit_price,
        )

        # ── P1-6: Circuit breaker — halt trading if daily loss exceeded ──
        can_trade, cb_reason = self.circuit_breaker.check()
        if not can_trade:
            log.critical("Circuit breaker blocked order: %s", cb_reason)
            return Execution(
                signal_id=signal.id,
                order_type="limit",
                requested_price=signal.limit_price,
                status=OrderStatus.REJECTED.value,
                error_message=cb_reason,
            )

        # ── P0-4: Duplicate order protection ─────────────────────────────
        if self._is_duplicate(signal):
            log.warning(
                "Duplicate detected for signal #%s (%s %s $%s %s) — skipping",
                signal.id, signal.action, signal.symbol,
                signal.strike or "", signal.expiration_date or "",
            )
            return Execution(
                signal_id=signal.id,
                order_type="limit",
                requested_price=signal.limit_price,
                status=OrderStatus.REJECTED.value,
                error_message="Duplicate order detected — skipped",
            )

        # ── P0-2: Buying power check ─────────────────────────────────────
        bp_ok, bp_msg = self._check_buying_power(signal)
        if not bp_ok:
            log.error("Insufficient buying power for signal #%s: %s", signal.id, bp_msg)
            if signal.id:
                db.update_signal(signal.id, status=SignalStatus.DENIED.value)
            return Execution(
                signal_id=signal.id,
                order_type="limit",
                requested_price=signal.limit_price,
                status=OrderStatus.REJECTED.value,
                error_message=bp_msg,
            )

        # Paper trading goes through Alpaca's paper API (real simulator, real fills)
        # NOT through _execute_paper() which only creates local DB records.
        # The AlpacaBroker is already initialized with paper=True keys.

        # ── P0-5: Use market orders for urgent (stop-loss) signals ────────
        if signal.urgency == Urgency.URGENT.value and hasattr(self.broker, "market_close_option"):
            order = self._dispatch_market_order(signal)
        else:
            order = self._dispatch_order(signal)

        is_market = signal.urgency == Urgency.URGENT.value and hasattr(self.broker, "market_close_option")
        execution = Execution(
            signal_id=signal.id,
            robinhood_order_id=order.order_id,
            secondary_order_id=getattr(order, "secondary_order_id", None),
            order_type="market" if is_market else "limit",
            requested_price=signal.limit_price,
            status=OrderStatus.PENDING.value if order.status != "failed" else OrderStatus.REJECTED.value,
            error_message=None if order.status != "failed" else "Order submission failed",
        )
        execution.id = db.create_execution(execution)

        if order.status == "failed":
            log.error("Order failed for signal #%s", signal.id)
        else:
            log.info(
                "Order placed: order_id=%s status=%s for signal #%s",
                order.order_id, order.status, signal.id,
            )
            # Create a provisional position record so the dashboard shows it immediately.
            # The order tracker will update it with fill details when the order fills.
            if signal.action in (SignalAction.SELL_CSP.value, SignalAction.SELL_CC.value):
                import uuid
                today = now_et().strftime("%Y-%m-%d")
                prov_pos = Position(
                    symbol=signal.symbol,
                    strategy=signal.strategy or "wheel_csp",
                    state="open",
                    option_type=signal.option_type,
                    strike=signal.strike,
                    expiration_date=signal.expiration_date,
                    quantity=1,
                    entry_date=today,
                    entry_price=signal.limit_price or 0.0,
                    entry_credit_total=(signal.limit_price or 0.0) * 100,
                    target_close_price=(signal.limit_price or 0.0) * get("wheel.profit_target_pct", 0.5),
                    stop_loss_price=(signal.limit_price or 0.0) * get("wheel.stop_loss_multiplier", 2.0),
                    ai_reasoning=signal.reason,
                    notes=f"Order pending: {order.order_id}",
                )
                prov_pos.id = db.create_position(prov_pos)
                execution.position_id = prov_pos.id
                db.update_execution(execution.id, position_id=prov_pos.id)
                log.info("Provisional position #%d created for %s", prov_pos.id, signal.symbol)

        return execution

    def execute_auto_exit(self, signal: Signal, position: Position) -> Execution:
        """Execute an auto-exit (e.g. profit target) and close the position.

        Identical to :meth:`execute_signal` but additionally closes the
        position in the database once the order is placed.
        """
        log.info(
            "Auto-exit: %s %s position #%d (reason: %s)",
            signal.action, signal.symbol, position.id or 0, signal.reason,
        )

        execution = self.execute_signal(signal)
        execution.position_id = position.id

        if execution.id:
            db.update_execution(execution.id, position_id=position.id)

        # For paper trades the fill is immediate — close the position now.
        # For live trades the OrderTracker will close on fill confirmation.
        if self.paper_trade and position.id:
            exit_price = signal.limit_price or 0.0
            db.close_position(position.id, exit_price=exit_price, exit_reason=signal.reason)
            log.info("Paper-trade: position #%d closed at $%.2f", position.id, exit_price)

        return execution

    # ── Internals ─────────────────────────────────────────────────────────

    def _dispatch_order(self, signal: Signal):
        """Route *signal* to the correct broker method and return the Order."""
        symbol = signal.symbol
        strike = signal.strike or 0.0
        expiration = signal.expiration_date or ""
        option_type = signal.option_type or "call"
        quantity = 1
        price = signal.limit_price or 0.0

        action = signal.action

        # VRP spread — multi-leg order
        if signal.strategy == "vrp_spread" and action == SignalAction.SELL_CSP.value:
            # Parse long strike from reason text, or compute it
            spread_width = get("vrp_spreads.spread_width", 5.0)
            long_strike = strike - spread_width
            if hasattr(self.broker, "sell_put_spread"):
                return self.broker.sell_put_spread(
                    symbol=symbol,
                    short_strike=strike,
                    long_strike=long_strike,
                    expiration=expiration,
                    quantity=quantity,
                    credit=price,
                )

        if action in (
            SignalAction.SELL_SHORT_CALL.value,
            SignalAction.SELL_CSP.value,
            SignalAction.SELL_CC.value,
        ):
            return self.broker.sell_to_open(
                symbol=symbol,
                strike=strike,
                expiration=expiration,
                option_type=option_type,
                quantity=quantity,
                price=price,
            )

        if action == SignalAction.BUY_TO_CLOSE.value:
            # VRP spread close — use atomic close_put_spread() to close BOTH legs
            if signal.strategy == "vrp_spread" and hasattr(self.broker, "close_put_spread"):
                spread_width = get("vrp_spreads.spread_width", 5.0)
                long_strike = strike - spread_width
                return self.broker.close_put_spread(
                    symbol=symbol,
                    short_strike=strike,
                    long_strike=long_strike,
                    expiration=expiration,
                    quantity=quantity,
                    debit=price,
                )
            # Single-leg close
            return self.broker.buy_to_close(
                symbol=symbol,
                strike=strike,
                expiration=expiration,
                option_type=option_type,
                quantity=quantity,
                price=price,
            )

        if action == SignalAction.BUY_LEAPS.value:
            return self.broker.buy_to_open(
                symbol=symbol,
                strike=strike,
                expiration=expiration,
                option_type=option_type,
                quantity=quantity,
                price=price,
            )

        if action == SignalAction.ROLL.value:
            # Roll = buy-to-close old, then sell-to-open new.
            # The signal for a roll is the buy-to-close leg; the new sell
            # is generated separately by the strategy scanner.
            return self.broker.buy_to_close(
                symbol=symbol,
                strike=strike,
                expiration=expiration,
                option_type=option_type,
                quantity=quantity,
                price=price,
            )

        log.warning("Unhandled signal action '%s' — defaulting to buy_to_open", action)
        return self.broker.buy_to_open(
            symbol=symbol,
            strike=strike,
            expiration=expiration,
            option_type=option_type,
            quantity=quantity,
            price=price,
        )

    def _execute_paper(self, signal: Signal) -> Execution:
        """Simulate order execution for paper trading.

        Creates BOTH Execution AND Position records so the dashboard,
        exit engine, and performance tracker all see the trade.
        For spreads (VRP), creates two linked positions (short + long leg).
        """
        import uuid

        now = now_et()
        today = now.strftime("%Y-%m-%d")
        fill_price = signal.limit_price or 0.0

        log.info(
            "[PAPER] %s %s %s $%s %s x1 @ $%.2f",
            signal.action, signal.symbol, signal.option_type or "",
            signal.strike or "", signal.expiration_date or "",
            fill_price,
        )

        # Create the Execution record
        execution = Execution(
            signal_id=signal.id,
            robinhood_order_id=f"PAPER-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
            order_type="limit",
            requested_price=signal.limit_price,
            fill_price=fill_price,
            fill_date=now.isoformat(),
            slippage=0.0,
            status=OrderStatus.FILLED.value,
        )
        execution.id = db.create_execution(execution)

        # Create Position record(s) — this is what the dashboard and exit engine read
        if signal.strategy == "vrp_spread":
            # VRP spread = two linked positions (short put + long put)
            pair_id = f"spread-{uuid.uuid4().hex[:8]}"
            spread_width = get("vrp_spreads.spread_width", 5.0)
            short_strike = signal.strike or 0.0
            long_strike = short_strike - spread_width
            credit = fill_price

            # Short leg (the one we sold — generates income)
            short_pos = Position(
                symbol=signal.symbol,
                strategy="vrp_spread",
                pair_id=pair_id,
                state="open",
                option_type="put",
                strike=short_strike,
                expiration_date=signal.expiration_date,
                quantity=1,
                entry_date=today,
                entry_price=credit,
                entry_credit_total=credit * 100,
                target_close_price=credit * 0.5,
                stop_loss_price=credit * 2.0,
                cost_basis=credit,
                ai_reasoning=signal.reason,
            )
            short_pos.id = db.create_position(short_pos)

            # Long leg (the one we bought — protection)
            long_pos = Position(
                symbol=signal.symbol,
                strategy="vrp_spread",
                pair_id=pair_id,
                state="open",
                option_type="put",
                strike=long_strike,
                expiration_date=signal.expiration_date,
                quantity=1,
                entry_date=today,
                entry_price=0.0,
                entry_credit_total=0.0,
            )
            long_pos.id = db.create_position(long_pos)

            execution.position_id = short_pos.id
            db.update_execution(execution.id, position_id=short_pos.id)

            log.info(
                "[PAPER] Spread created: pair=%s, short $%.0f put, long $%.0f put, credit $%.2f",
                pair_id, short_strike, long_strike, credit,
            )

        elif signal.action in (SignalAction.BUY_TO_CLOSE.value, SignalAction.ROLL.value):
            # Closing an existing position — DON'T create new positions.
            # execute_auto_exit() handles closing the position in the DB.
            log.info("[PAPER] Close order for %s %s $%s — position will be closed by exit engine",
                     signal.symbol, signal.option_type or "", signal.strike or "")

        else:
            # Single-leg position (CSP, CC, LEAPS, etc.)
            # Use strategy-appropriate config for targets/stops
            if signal.strategy and signal.strategy.startswith("wheel"):
                target_pct = get("wheel.profit_target_pct", 0.5)
                stop_mult = get("wheel.stop_loss_multiplier", 2.0)
            else:
                target_pct = get("vrp_spreads.profit_target_pct", 0.5)
                stop_mult = get("vrp_spreads.stop_loss_multiplier", 2.0)

            pos = Position(
                symbol=signal.symbol,
                strategy=signal.strategy,
                state="open",
                option_type=signal.option_type,
                strike=signal.strike,
                expiration_date=signal.expiration_date,
                quantity=1,
                entry_date=today,
                entry_price=fill_price,
                entry_credit_total=fill_price * 100,
                target_close_price=fill_price * target_pct,
                stop_loss_price=fill_price * stop_mult,
                ai_reasoning=signal.reason,
            )
            pos.id = db.create_position(pos)
            execution.position_id = pos.id
            db.update_execution(execution.id, position_id=pos.id)

            log.info("[PAPER] Position #%d created for %s", pos.id, signal.symbol)

        log.info("[PAPER] Execution #%d filled at $%.2f", execution.id or 0, fill_price)
        return execution

    # ── P0-2: Buying power guard ──────────────────────────────────────────

    def _check_buying_power(self, signal: Signal) -> tuple[bool, str]:
        """Verify sufficient buying power before placing an order.

        For spreads, required capital = spread_width * 100.
        For other orders, required capital = limit_price * 100.
        Returns (ok, message).
        """
        try:
            if self.paper_trade:
                buying_power = self.broker.get_buying_power()
            else:
                buying_power = self.broker.get_buying_power()
        except Exception as e:
            log.warning("Could not check buying power: %s — allowing order", e)
            return True, "buying power check skipped (API error)"

        # Calculate required capital
        if signal.action in (SignalAction.SELL_CSP.value, SignalAction.SELL_CC.value):
            required = (signal.strike or 0.0) * 100  # CSP/CC collateral = strike * 100
        elif signal.strategy == "vrp_spread":
            spread_width = get("vrp_spreads.spread_width", 5.0)
            required = spread_width * 100  # e.g., $5 * 100 = $500
        else:
            required = (signal.limit_price or 0.0) * 100  # For buys, use the cost

        if buying_power < required:
            msg = (
                f"Buying power ${buying_power:.2f} < required ${required:.2f} "
                f"(signal: {signal.action} {signal.symbol} ${signal.strike or 0:.0f})"
            )
            return False, msg

        log.info(
            "Buying power OK: $%.2f available, $%.2f required",
            buying_power, required,
        )
        return True, "sufficient"

    # ── P0-4: Duplicate order protection ──────────────────────────────────

    def _is_duplicate(self, signal: Signal) -> bool:
        """Check if this signal would create a duplicate order.

        Returns True if:
          1. There is already a pending execution for this signal_id, OR
          2. There is already an open position matching this exact
             symbol + strike + expiration.
        """
        # Check 1: pending execution for the same signal
        if signal.id:
            pending = db.get_pending_executions()
            for exe in pending:
                if exe.signal_id == signal.id:
                    log.warning(
                        "Duplicate: pending execution #%s already exists for signal #%s",
                        exe.id, signal.id,
                    )
                    return True

        # Check 2: open position with same symbol + strike + expiration
        open_positions = db.get_open_positions()
        for pos in open_positions:
            if (
                pos.symbol == signal.symbol
                and pos.strike == signal.strike
                and pos.expiration_date == signal.expiration_date
                and pos.option_type == signal.option_type
            ):
                # Allow buy-to-close on existing positions (that's intentional)
                if signal.action in (
                    SignalAction.BUY_TO_CLOSE.value,
                    SignalAction.ROLL.value,
                ):
                    continue
                log.warning(
                    "Duplicate: open position #%s matches %s %s $%s %s",
                    pos.id, signal.symbol, signal.option_type,
                    signal.strike, signal.expiration_date,
                )
                return True

        return False

    # ── P0-5: Market order dispatch for urgent signals ────────────────────

    def _dispatch_market_order(self, signal: Signal):
        """Route urgent *signal* to the broker's market_close_option method."""
        symbol = signal.symbol
        strike = signal.strike or 0.0
        expiration = signal.expiration_date or ""
        option_type = signal.option_type or "call"
        quantity = 1

        log.info(
            "URGENT: Using market order for stop-loss: %s %s $%s %s",
            signal.symbol, option_type, strike, expiration,
        )
        return self.broker.market_close_option(
            symbol=symbol,
            strike=strike,
            expiration=expiration,
            option_type=option_type,
            quantity=quantity,
        )
