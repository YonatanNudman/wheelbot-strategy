"""Connection health monitor — pings Robinhood and alerts via Discord on failure."""

from datetime import datetime

import requests

from utils.logger import get_logger
from utils.timing import format_et, now_et

logger = get_logger("utils.heartbeat")


class HeartbeatMonitor:
    """Periodically pings the broker and sends Discord webhook alerts on outages.

    Usage::

        monitor = HeartbeatMonitor(broker=robin_broker, webhook_url="https://discord.com/...")
        monitor.check()   # call on each bot loop iteration
    """

    def __init__(self, broker: object, webhook_url: str) -> None:
        self._broker = broker
        self._webhook_url: str = webhook_url

        self.consecutive_failures: int = 0
        self.last_success_time: datetime | None = None
        self.total_uptime_pct: float = 100.0

        self._total_checks: int = 0
        self._successful_checks: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Attempt a lightweight broker call. Returns True on success."""
        try:
            self._broker.get_buying_power()
            return True
        except Exception as exc:
            logger.warning("Heartbeat ping failed: %s", exc)
            return False

    def check(self) -> None:
        """Run a ping and handle failure/recovery logic.

        * On success — resets the failure counter, records the time.
        * On 2+ consecutive failures — sends a Discord alert and attempts
          a session refresh via ``broker.auth.refresh_session()``.
        * On successful reconnect — sends a recovery alert.
        """
        self._total_checks += 1
        alive = self.ping()

        if alive:
            self._record_success()
            return

        self.consecutive_failures += 1
        logger.error(
            "Heartbeat failure #%d (consecutive)", self.consecutive_failures
        )

        if self.consecutive_failures >= 2:
            self._handle_outage()

        self._update_uptime()

    # ------------------------------------------------------------------
    # Discord webhook
    # ------------------------------------------------------------------

    def _send_webhook_alert(self, message: str) -> None:
        """POST a message to the configured Discord webhook."""
        try:
            response = requests.post(
                self._webhook_url,
                json={"content": message},
                timeout=10,
            )
            response.raise_for_status()
            logger.info("Discord alert sent: %s", message)
        except Exception as exc:
            logger.error("Failed to send Discord alert: %s", exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _record_success(self) -> None:
        """Handle a successful ping."""
        if self.consecutive_failures > 0:
            logger.info(
                "Broker connection restored after %d failures",
                self.consecutive_failures,
            )
        self.consecutive_failures = 0
        self.last_success_time = now_et()
        self._successful_checks += 1
        self._update_uptime()

    def _handle_outage(self) -> None:
        """Alert and attempt reconnection."""
        timestamp = format_et(now_et())
        self._send_webhook_alert(
            f"⚠️ Bot lost connection to Robinhood at {timestamp}. "
            "Positions unmonitored. Attempting reconnect..."
        )

        reconnected = self._attempt_reconnect()

        if reconnected:
            recovery_time = format_et(now_et())
            self._send_webhook_alert(
                f"✅ Reconnected to Robinhood at {recovery_time}"
            )
            self._record_success()

    def _attempt_reconnect(self) -> bool:
        """Try refreshing the broker session."""
        try:
            self._broker.auth.refresh_session()
            logger.info("Broker session refreshed successfully")
            return True
        except Exception as exc:
            logger.error("Reconnect attempt failed: %s", exc)
            return False

    def _update_uptime(self) -> None:
        """Recalculate total uptime percentage."""
        if self._total_checks > 0:
            self.total_uptime_pct = (
                self._successful_checks / self._total_checks
            ) * 100
