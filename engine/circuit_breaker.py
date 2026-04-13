"""CircuitBreaker — max daily loss protection to halt trading when losses exceed threshold."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from data import database as db
from utils.config import get
from utils.logger import get_logger
from utils.timing import now_et

if TYPE_CHECKING:
    from discord_bot.webhook import WebhookSender

log = get_logger(__name__)


class CircuitBreaker:
    """Tracks realized losses for the current day and halts trading if they exceed a threshold.

    The threshold is expressed as a percentage of portfolio value (default 5%).
    Resets automatically at midnight ET.
    """

    def __init__(self, webhook: object | None = None) -> None:
        self._threshold_pct: float = get("risk.max_daily_loss_pct", 0.05)
        self._webhook = webhook
        self._tripped_date: date | None = None  # Date when breaker was last tripped

    def check(self) -> tuple[bool, str]:
        """Check whether trading is permitted.

        Returns:
            (can_trade, reason) -- False + reason string when the breaker is tripped.
        """
        today = now_et().date()

        # Reset at midnight ET — if tripped date is in the past, clear it
        if self._tripped_date is not None and self._tripped_date < today:
            log.info("Circuit breaker reset — new trading day")
            self._tripped_date = None

        # If already tripped today, stay tripped
        if self._tripped_date == today:
            reason = (
                f"Circuit breaker TRIPPED: daily loss exceeded "
                f"{self._threshold_pct:.0%} threshold. Trading halted for today."
            )
            return False, reason

        # Calculate realized losses for today from closed positions
        # AND unrealized losses from open positions
        realized = self._get_today_realized_loss(today)
        unrealized = self._get_unrealized_loss()
        # Combine: realized P&L + unrealized losses only (not unrealized gains)
        total_exposure = realized + min(0, unrealized)
        portfolio_value = self._get_portfolio_value()

        if portfolio_value <= 0:
            return True, ""

        loss_pct = abs(total_exposure) / portfolio_value if total_exposure < 0 else 0.0

        if loss_pct >= self._threshold_pct:
            self._tripped_date = today
            reason = (
                f"Circuit breaker TRIPPED: total exposure ${total_exposure:.2f} "
                f"(realized ${realized:.2f} + unrealized ${min(0, unrealized):.2f}) "
                f"= {loss_pct:.1%} of portfolio ${portfolio_value:.2f} "
                f"exceeds {self._threshold_pct:.0%} threshold. "
                f"All trading halted until midnight ET."
            )
            log.critical(reason)
            self._send_alert(reason)
            return False, reason

        return True, ""

    def _get_unrealized_loss(self) -> float:
        """Sum P&L of all open positions. Negative = unrealized loss.

        The exit engine updates pnl_dollars on open positions every ~5 min,
        so this reflects near-real-time unrealized P&L.
        """
        open_positions = db.get_open_positions()
        unrealized_pnl = 0.0
        for pos in open_positions:
            if pos.pnl_dollars is not None:
                unrealized_pnl += pos.pnl_dollars
        return unrealized_pnl

    def _get_today_realized_loss(self, today: date) -> float:
        """Sum P&L of all positions closed today. Negative = loss."""
        today_str = today.isoformat()
        closed_positions = db.get_closed_trades()
        daily_pnl = 0.0
        for pos in closed_positions:
            if pos.exit_date == today_str and pos.pnl_dollars is not None:
                daily_pnl += pos.pnl_dollars
        return daily_pnl

    def _get_portfolio_value(self) -> float:
        """Get current portfolio value from the latest snapshot or config fallback."""
        snapshot = db.get_latest_snapshot()
        if snapshot and snapshot.total_account_value > 0:
            return snapshot.total_account_value
        # Fallback to configured capital
        return float(get("capital.total", 5000))

    def _send_alert(self, message: str) -> None:
        """Send a critical alert via webhook."""
        if self._webhook and hasattr(self._webhook, "send"):
            try:
                self._webhook.send(f"CIRCUIT BREAKER: {message}")
            except Exception as exc:
                log.error("Circuit breaker webhook alert failed: %s", exc)
