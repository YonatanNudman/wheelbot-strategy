"""Signal dataclass and approval queue management."""

from __future__ import annotations

from data import database as db
from data.models import Signal, SignalStatus
from utils.logger import get_logger
from utils.timing import now_et

log = get_logger(__name__)


class SignalQueue:
    """Manages the signal approval queue — create, approve, deny, expire signals."""

    def get_pending(self) -> list[Signal]:
        """Get all signals awaiting user approval."""
        return db.get_pending_signals()

    def create(self, signal: Signal) -> int:
        """Add a new signal to the queue. Returns signal ID."""
        signal_id = db.create_signal(signal)
        log.info(
            "Signal created: #%d %s %s %s $%s %s",
            signal_id, signal.action, signal.symbol,
            signal.option_type or "", signal.strike or "", signal.expiration_date or "",
        )
        return signal_id

    def approve(self, signal_id: int) -> None:
        """Mark a signal as approved (user clicked Approve)."""
        db.update_signal(
            signal_id,
            status=SignalStatus.APPROVED.value,
            approved_at=now_et().isoformat(),
        )
        log.info("Signal #%d approved", signal_id)

    def deny(self, signal_id: int) -> None:
        """Mark a signal as denied (user clicked Deny)."""
        db.update_signal(signal_id, status=SignalStatus.DENIED.value)
        log.info("Signal #%d denied", signal_id)

    def mark_executed(self, signal_id: int) -> None:
        """Mark a signal as successfully executed."""
        db.update_signal(
            signal_id,
            status=SignalStatus.EXECUTED.value,
            executed_at=now_et().isoformat(),
        )
        log.info("Signal #%d executed", signal_id)

    def mark_auto_executed(self, signal_id: int) -> None:
        """Mark a signal as auto-executed (e.g., profit target hit)."""
        db.update_signal(
            signal_id,
            status=SignalStatus.AUTO_EXECUTED.value,
            executed_at=now_et().isoformat(),
        )
        log.info("Signal #%d auto-executed", signal_id)

    def expire_stale(self) -> int:
        """Expire signals past their valid_until time. Returns count expired."""
        pending = self.get_pending()
        now = now_et().isoformat()
        count = 0
        for sig in pending:
            if sig.valid_until and sig.valid_until < now:
                db.update_signal(sig.id, status=SignalStatus.EXPIRED.value)
                log.info("Signal #%d expired (valid_until: %s)", sig.id, sig.valid_until)
                count += 1
        return count
