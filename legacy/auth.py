"""Resilient Robinhood authentication with TOTP MFA and session caching."""

from __future__ import annotations

import os
import time
from typing import Optional

import pyotp
import robin_stocks.robinhood as rh

from utils.logger import get_logger

logger = get_logger("broker.auth")

MAX_LOGIN_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 30


def _get_credentials() -> tuple[str, str, str]:
    """Load Robinhood credentials from environment variables.

    Returns:
        Tuple of (username, password, totp_secret).

    Raises:
        EnvironmentError: If any required variable is missing.
    """
    username = os.environ.get("RH_USERNAME")
    password = os.environ.get("RH_PASSWORD")
    totp_secret = os.environ.get("RH_TOTP_SECRET")

    missing = [
        name
        for name, val in [
            ("RH_USERNAME", username),
            ("RH_PASSWORD", password),
            ("RH_TOTP_SECRET", totp_secret),
        ]
        if not val
    ]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    return username, password, totp_secret  # type: ignore[return-value]


def _generate_totp(secret: str) -> str:
    """Generate a fresh TOTP code from the shared secret."""
    totp = pyotp.TOTP(secret)
    return totp.now()


def is_session_valid() -> bool:
    """Lightweight check: try to fetch buying power. If it works, session is alive."""
    try:
        profile = rh.profiles.load_account_profile()
        if profile and profile.get("buying_power") is not None:
            return True
    except Exception:
        pass
    return False


def login() -> bool:
    """Authenticate with Robinhood using TOTP MFA.

    Checks for a valid existing session first. On failure, retries up to
    MAX_LOGIN_ATTEMPTS times with a fresh TOTP code each attempt.

    Returns:
        True on success, False on total failure.
    """
    if is_session_valid():
        logger.info("Existing Robinhood session is still valid — skipping login")
        return True

    try:
        username, password, totp_secret = _get_credentials()
    except EnvironmentError as exc:
        logger.error("Credential loading failed: %s", exc)
        return False

    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        mfa_code = _generate_totp(totp_secret)
        logger.info(
            "Login attempt %d/%d for %s", attempt, MAX_LOGIN_ATTEMPTS, username
        )

        try:
            result = rh.login(
                username,
                password,
                mfa_code=mfa_code,
                store_session=True,
            )
            if result and "access_token" in result:
                logger.info("Robinhood login successful")
                return True

            logger.warning("Login returned unexpected payload: %s", result)

        except Exception as exc:
            logger.warning("Login attempt %d failed: %s", attempt, exc)

        if attempt < MAX_LOGIN_ATTEMPTS:
            logger.info("Waiting %ds before retry…", RETRY_DELAY_SECONDS)
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error(
        "All %d login attempts exhausted — caller should send webhook alert",
        MAX_LOGIN_ATTEMPTS,
    )
    return False


def refresh_session() -> bool:
    """Force a full re-authentication, ignoring cached sessions."""
    logger.info("Forcing session refresh")
    try:
        rh.logout()
    except Exception:
        pass  # best-effort logout before re-auth
    return login()
