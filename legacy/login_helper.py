"""Interactive Robinhood login helper.

Run this script directly to:
1. Log in with your email/password
2. Enter the MFA code Robinhood sends you
3. Cache the session so the bot can reuse it

Usage:
  python login_helper.py
  python login_helper.py --code 123456
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

import robin_stocks.robinhood as r


def main():
    username = os.getenv("RH_USERNAME")
    password = os.getenv("RH_PASSWORD")
    totp_secret = os.getenv("RH_TOTP_SECRET")

    if not username or not password:
        print("ERROR: RH_USERNAME and RH_PASSWORD must be set in .env")
        sys.exit(1)

    print(f"Logging in as {username}...")

    # If we have a TOTP secret, use it for automatic MFA
    if totp_secret:
        import pyotp

        totp = pyotp.TOTP(totp_secret)
        mfa_code = totp.now()
        print(f"Generated TOTP code: {mfa_code}")
        login = r.login(username, password, mfa_code=mfa_code, store_session=True)
    else:
        # No TOTP — Robinhood will send a code via email/SMS
        # robin_stocks will prompt for it on stdin
        print("No TOTP secret configured. Robinhood will send you a verification code.")
        print("Check your email/phone and enter the code when prompted.")
        print()
        login = r.login(username, password, store_session=True)

    if login:
        print()
        print("Login successful!")
        print("Session cached — the bot can reuse this session.")

        # Verify with a basic call
        try:
            profile = r.load_account_profile()
            buying_power = profile.get("buying_power", "unknown")
            print(f"Buying power: ${buying_power}")
            print(f"Account number: {profile.get('account_number', 'N/A')}")
        except Exception as e:
            print(f"Warning: Could not fetch profile: {e}")
    else:
        print("Login failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
