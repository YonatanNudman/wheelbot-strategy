"""Lightweight webhook sender for WheelBot Discord alerts."""

from __future__ import annotations

import requests

from utils.logger import get_logger

log = get_logger(__name__)


class WebhookSender:
    """Send messages and embeds to a Discord webhook URL (synchronous)."""

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ── Public API ────────────────────────────────────────────────────────

    def send(self, content: str) -> bool:
        """POST a plain-text message to the webhook. Returns True on success."""
        payload = {"content": content}
        return self._post(payload)

    def send_embed(self, embed_dict: dict) -> bool:
        """POST a rich embed to the webhook.

        Parameters
        ----------
        embed_dict:
            A dict representing a Discord embed (title, description, fields, etc.)
            — typically obtained via ``discord.Embed.to_dict()``.
        """
        payload = {"embeds": [embed_dict]}
        return self._post(payload)

    def send_error(self, error_msg: str) -> bool:
        """Send a formatted error alert via webhook."""
        embed_dict = {
            "title": "🔴 WheelBot Error",
            "description": error_msg,
            "color": 0xFF0000,  # Red
        }
        return self.send_embed(embed_dict)

    def send_heartbeat_alert(self, message: str) -> bool:
        """Send a heartbeat / connection-issue alert via webhook."""
        embed_dict = {
            "title": "💓 Heartbeat Alert",
            "description": message,
            "color": 0xFFA500,  # Orange
        }
        return self.send_embed(embed_dict)

    # ── Internals ─────────────────────────────────────────────────────────

    def _post(self, payload: dict) -> bool:
        """Execute the HTTP POST and handle errors."""
        try:
            resp = self._session.post(self._url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                return True

            log.warning(
                "Webhook POST returned %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return False

        except requests.RequestException as exc:
            log.error("Webhook POST failed: %s", exc)
            return False
