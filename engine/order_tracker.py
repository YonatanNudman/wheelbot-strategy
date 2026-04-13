"""OrderTracker — monitors pending orders and handles stale/cancelled states."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from data import database as db
from data.models import Execution, OrderStatus, Position, PositionState, SignalAction
from utils.config import get
from utils.logger import get_logger
from utils.timing import now_et

if TYPE_CHECKING:
    from broker.alpaca_broker import AlpacaBroker

log = get_logger(__name__)


class OrderTracker:
    """Polls broker for order status updates and reconciles with the DB.

    Designed to run on a schedule (every ~5 minutes).  When an order fills,
    the tracker updates the Execution record and the linked Position.
    """

    def __init__(self, broker: AlpacaBroker, webhook: object | None = None) -> None:
        self.broker = broker
        self.webhook = webhook
        self.stale_hours: float = get("scheduling.stale_order_hours", 2.0)

    # ── Public API ────────────────────────────────────────────────────────

    def check_pending_orders(self) -> None:
        """Iterate over every pending Execution and sync its status with the broker."""
        pending = db.get_pending_executions()
        if not pending:
            log.debug("No pending executions to check")
            return

        log.info("Checking %d pending execution(s)", len(pending))
        now = now_et()

        for exe in pending:
            if not exe.robinhood_order_id or exe.robinhood_order_id.startswith("PAPER"):
                continue

            try:
                order = self.broker.get_order_status(exe.robinhood_order_id)
            except Exception as exc:
                log.error(
                    "Failed to fetch status for order %s (exec #%s): %s",
                    exe.robinhood_order_id, exe.id, exc,
                )
                continue

            status = order.status.lower()

            if status in ("filled", "confirmed"):
                self._handle_filled(exe, order)
            elif status in ("cancelled", "canceled", "rejected"):
                self._handle_cancelled(exe, order)
            else:
                self._handle_still_pending(exe, order, now)

    def cancel_all_pending(self) -> int:
        """Cancel every unfilled order (e.g. the 3:50 PM sweep).

        Returns:
            Number of orders successfully cancelled.
        """
        pending = db.get_pending_executions()
        if not pending:
            log.info("No pending orders to cancel")
            return 0

        cancelled = 0
        for exe in pending:
            if not exe.robinhood_order_id or exe.robinhood_order_id.startswith("PAPER"):
                continue

            success = self.broker.cancel_order(exe.robinhood_order_id)
            if success:
                db.update_execution(
                    exe.id,
                    status=OrderStatus.CANCELLED.value,
                )
                cancelled += 1
                log.info("Cancelled order %s (exec #%s)", exe.robinhood_order_id, exe.id)
            else:
                log.warning(
                    "Failed to cancel order %s (exec #%s)",
                    exe.robinhood_order_id, exe.id,
                )

        log.info("End-of-day cancel sweep: %d/%d cancelled", cancelled, len(pending))
        return cancelled

    # ── Internal handlers ─────────────────────────────────────────────────

    def _handle_filled(self, exe: Execution, order) -> None:
        """Process an order that has been filled.

        For opening trades (no position_id yet), creates Position records
        using the actual fill data from the broker.
        For closing trades (position_id exists), closes the position.
        """
        fill_price = order.fill_price or order.limit_price
        slippage = 0.0
        if exe.requested_price and fill_price:
            slippage = round(fill_price - exe.requested_price, 4)

        fill_date = order.fill_date.isoformat() if order.fill_date else now_et().isoformat()

        db.update_execution(
            exe.id,
            status=OrderStatus.FILLED.value,
            fill_price=fill_price,
            fill_date=fill_date,
            slippage=slippage,
        )

        # If this execution already has a linked position, handle closing
        if exe.position_id:
            position = db.get_position(exe.position_id)
            if position and position.state == PositionState.OPEN.value:
                # If this was a closing order, close the position
                if order.action == "buy" and position.option_type in ("call", "put"):
                    db.close_position(
                        exe.position_id,
                        exit_price=fill_price,
                        exit_reason="order_filled",
                    )
                    log.info(
                        "Position #%d closed at $%.4f (order filled)",
                        exe.position_id, fill_price,
                    )
        else:
            # No position_id — this is an opening trade fill in live mode.
            # Create Position record(s) from the fill data.
            self._create_position_from_fill(exe, order, fill_price, fill_date)

        log.info(
            "Order %s FILLED at $%.4f (slippage: $%.4f) — exec #%s",
            exe.robinhood_order_id, fill_price, slippage, exe.id,
        )

    def _create_position_from_fill(
        self, exe: Execution, order, fill_price: float, fill_date: str,
    ) -> None:
        """Create Position record(s) in the DB when a live opening trade fills.

        For VRP spreads: creates both short and long legs with the same pair_id.
        For single-leg orders: creates one position.
        """
        import uuid

        today = now_et().strftime("%Y-%m-%d")

        # Determine the signal to understand the strategy
        signal = None
        if exe.signal_id:
            from data.models import Signal
            sig_row = db.get_pending_signals()  # Check all signals
            # Look up the signal directly
            try:
                from data import database as db_mod
                with db_mod._connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM signals WHERE id = ?", (exe.signal_id,)
                    ).fetchone()
                if row:
                    signal = Signal(**{k: row[k] for k in row.keys()})
            except Exception as exc:
                log.warning("Could not fetch signal #%s for position creation: %s", exe.signal_id, exc)

        if signal and signal.strategy == "vrp_spread":
            # VRP spread: create two linked positions
            pair_id = f"spread-{uuid.uuid4().hex[:8]}"
            spread_width = get("vrp_spreads.spread_width", 5.0)
            short_strike = signal.strike or 0.0
            long_strike = short_strike - spread_width

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
                entry_price=fill_price,
                entry_credit_total=fill_price * 100,
                target_close_price=fill_price * 0.5,
                stop_loss_price=fill_price * 2.0,
                cost_basis=fill_price,
                ai_reasoning=signal.reason,
            )
            short_pos.id = db.create_position(short_pos)

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

            # Link execution to the short leg position
            db.update_execution(exe.id, position_id=short_pos.id)

            log.info(
                "Live fill: VRP spread created pair=%s, short $%.0f, long $%.0f, credit $%.2f",
                pair_id, short_strike, long_strike, fill_price,
            )

        elif signal and signal.action not in (
            SignalAction.BUY_TO_CLOSE.value, SignalAction.ROLL.value,
        ):
            # Single-leg opening position
            pos = Position(
                symbol=signal.symbol,
                strategy=signal.strategy or "",
                state="open",
                option_type=signal.option_type,
                strike=signal.strike,
                expiration_date=signal.expiration_date,
                quantity=1,
                entry_date=today,
                entry_price=fill_price,
                entry_credit_total=fill_price * 100,
                target_close_price=fill_price * get("vrp_spreads.profit_target_pct", 0.5),
                stop_loss_price=fill_price * get("vrp_spreads.stop_loss_multiplier", 2.0),
                ai_reasoning=signal.reason if signal else None,
            )
            pos.id = db.create_position(pos)
            db.update_execution(exe.id, position_id=pos.id)

            log.info(
                "Live fill: position #%d created for %s %s $%s",
                pos.id, signal.symbol, signal.option_type or "", signal.strike or "",
            )

    def _handle_cancelled(self, exe: Execution, order) -> None:
        """Process an order that was cancelled or rejected."""
        new_status = (
            OrderStatus.CANCELLED.value
            if "cancel" in order.status.lower()
            else OrderStatus.REJECTED.value
        )

        db.update_execution(
            exe.id,
            status=new_status,
            error_message=f"Order {order.status} by broker",
        )

        log.warning(
            "Order %s %s — exec #%s", exe.robinhood_order_id, new_status, exe.id,
        )

        self._send_alert(
            f"Order {new_status}: {order.symbol} "
            f"{order.option_type} ${order.strike} {order.expiration_date} "
            f"(exec #{exe.id})"
        )

    def _handle_still_pending(self, exe: Execution, order, now: datetime) -> None:
        """Check age of a still-pending order and alert if stale."""
        if not exe.created_at:
            return

        try:
            created = datetime.fromisoformat(exe.created_at)
        except (ValueError, TypeError):
            return

        # Make naive if necessary for subtraction
        if created.tzinfo is None:
            created = created.replace(tzinfo=now.tzinfo)

        age_hours = (now - created).total_seconds() / 3600

        if age_hours >= self.stale_hours:
            log.warning(
                "Order %s has been pending for %.1fh (threshold: %.1fh) — exec #%s",
                exe.robinhood_order_id, age_hours, self.stale_hours, exe.id,
            )
            self._send_alert(
                f"Stale order ({age_hours:.1f}h): {order.symbol} "
                f"{order.option_type} ${order.strike} {order.expiration_date} "
                f"@ ${order.limit_price:.2f} — consider adjusting price"
            )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _send_alert(self, message: str) -> None:
        """Dispatch an alert through the webhook, if configured."""
        if self.webhook and hasattr(self.webhook, "send"):
            try:
                self.webhook.send(message)
            except Exception as exc:
                log.error("Webhook alert failed: %s", exc)
        else:
            log.info("Alert (no webhook): %s", message)
