"""RobinhoodBroker — rate-limited, retry-aware wrapper around robin_stocks."""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Optional

import robin_stocks.robinhood as rh

from broker import auth
from broker.models import (
    AccountInfo,
    OptionContract,
    Order,
    Position,
    StockPosition,
    StockQuote,
)
from utils.logger import get_logger

logger = get_logger("broker.robinhood")

API_DELAY_SECONDS = 1.5
THROTTLE_RETRY_SECONDS = 15


def _rate_limit() -> None:
    """Pause between API calls to stay under Robinhood rate limits."""
    time.sleep(API_DELAY_SECONDS)


def _is_throttled(error: Exception) -> bool:
    """Check whether an exception looks like a Robinhood throttle/rate-limit error."""
    return "throttle" in str(error).lower()


class RobinhoodBroker:
    """High-level broker interface backed by Robinhood via robin_stocks."""

    def __init__(self) -> None:
        if not auth.login():
            logger.error("Failed to authenticate with Robinhood during init")
            raise RuntimeError("Robinhood authentication failed")
        logger.info("RobinhoodBroker initialised")

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account_info(self) -> AccountInfo:
        """Fetch account-level financial snapshot."""
        try:
            profile = rh.account.load_phoenix_account()
            _rate_limit()

            account_info = AccountInfo(
                buying_power=float(profile.get("account_buying_power", {}).get("amount", 0)),
                cash_balance=float(profile.get("uninvested_cash", {}).get("amount", 0)),
                portfolio_value=float(profile.get("total_equity", {}).get("amount", 0)),
                day_trade_count=int(profile.get("day_trade_counter", {}).get("counter", 0)),
            )
            logger.debug("Account info: buying_power=%.2f", account_info.buying_power)
            return account_info

        except Exception as exc:
            if _is_throttled(exc):
                logger.warning("Throttled on get_account_info — retrying in %ds", THROTTLE_RETRY_SECONDS)
                time.sleep(THROTTLE_RETRY_SECONDS)
                return self.get_account_info()
            raise

    def get_buying_power(self) -> float:
        """Return current buying power as a float."""
        return self.get_account_info().buying_power

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_stock_positions(self) -> list[StockPosition]:
        """Return all current equity positions."""
        try:
            raw_positions = rh.account.get_current_positions()
            _rate_limit()
        except Exception as exc:
            if _is_throttled(exc):
                logger.warning("Throttled on get_stock_positions — retrying in %ds", THROTTLE_RETRY_SECONDS)
                time.sleep(THROTTLE_RETRY_SECONDS)
                return self.get_stock_positions()
            raise

        positions: list[StockPosition] = []
        for pos in raw_positions or []:
            quantity = float(pos.get("quantity", 0))
            if quantity == 0:
                continue

            avg_price = float(pos.get("average_buy_price", 0))
            current_price = float(pos.get("current_price", 0) or 0)
            equity = quantity * current_price
            pct_change = (
                ((current_price - avg_price) / avg_price * 100) if avg_price else 0.0
            )

            # Resolve symbol from instrument URL if not directly available
            symbol = pos.get("symbol", "")
            if not symbol:
                try:
                    instrument_data = rh.stocks.get_instrument_by_url(
                        pos.get("instrument", "")
                    )
                    _rate_limit()
                    symbol = instrument_data.get("symbol", "UNKNOWN") if instrument_data else "UNKNOWN"
                except Exception:
                    symbol = "UNKNOWN"

            positions.append(
                StockPosition(
                    symbol=symbol,
                    quantity=quantity,
                    average_buy_price=avg_price,
                    current_price=current_price,
                    equity=equity,
                    percent_change=pct_change,
                )
            )

        logger.debug("Fetched %d stock positions", len(positions))
        return positions

    def get_option_positions(self) -> list[Position]:
        """Return all open option positions."""
        try:
            raw = rh.options.get_open_option_positions()
            _rate_limit()
        except Exception as exc:
            if _is_throttled(exc):
                logger.warning("Throttled on get_option_positions — retrying in %ds", THROTTLE_RETRY_SECONDS)
                time.sleep(THROTTLE_RETRY_SECONDS)
                return self.get_option_positions()
            raise

        positions: list[Position] = []
        for pos in raw or []:
            quantity = float(pos.get("quantity", 0))
            if quantity == 0:
                continue

            chain_symbol = pos.get("chain_symbol", "")
            option_type = pos.get("type", "unknown")
            strike = float(pos.get("strike_price", 0))
            exp_str = pos.get("expiration_date", "")
            avg_price = float(pos.get("average_price", 0)) / 100  # pennies → dollars
            current_price = float(pos.get("current_price", 0) or 0)

            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                exp_date = date.today()

            positions.append(
                Position(
                    symbol=chain_symbol,
                    option_type=option_type,
                    strike=strike,
                    expiration_date=exp_date,
                    quantity=quantity,
                    average_price=avg_price,
                    current_price=current_price,
                    option_id=pos.get("id", ""),
                )
            )

        logger.debug("Fetched %d open option positions", len(positions))
        return positions

    # ------------------------------------------------------------------
    # Option chains
    # ------------------------------------------------------------------

    def get_option_chain(
        self,
        symbol: str,
        expiration_date: Optional[str] = None,
    ) -> list[OptionContract]:
        """Fetch option chain for *symbol*, optionally filtered by expiration.

        Args:
            symbol: Underlying ticker (e.g. "AAPL").
            expiration_date: ISO date string "YYYY-MM-DD". If None, returns nearest expiration.
        """
        try:
            chain_info = rh.options.get_chains(symbol)
            _rate_limit()
        except Exception as exc:
            if _is_throttled(exc):
                logger.warning("Throttled on get_option_chain — retrying in %ds", THROTTLE_RETRY_SECONDS)
                time.sleep(THROTTLE_RETRY_SECONDS)
                return self.get_option_chain(symbol, expiration_date)
            raise

        if not chain_info or "id" not in chain_info:
            logger.warning("No option chain found for %s", symbol)
            return []

        # Determine expiration dates to query
        expirations = chain_info.get("expiration_dates", [])
        if expiration_date:
            if expiration_date not in expirations:
                logger.warning(
                    "Expiration %s not available for %s. Available: %s",
                    expiration_date,
                    symbol,
                    expirations[:5],
                )
                return []
            target_expirations = [expiration_date]
        else:
            target_expirations = expirations[:1]  # nearest only

        contracts: list[OptionContract] = []

        for exp in target_expirations:
            for opt_type in ("call", "put"):
                try:
                    options = rh.options.find_options_by_expiration(
                        [symbol],
                        expirationDate=exp,
                        optionType=opt_type,
                    )
                    _rate_limit()
                except Exception as exc:
                    if _is_throttled(exc):
                        logger.warning("Throttled fetching %s %s — retrying", exp, opt_type)
                        time.sleep(THROTTLE_RETRY_SECONDS)
                        options = rh.options.find_options_by_expiration(
                            [symbol],
                            expirationDate=exp,
                            optionType=opt_type,
                        )
                        _rate_limit()
                    else:
                        raise

                for opt in options or []:
                    try:
                        exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                    except (ValueError, TypeError):
                        exp_date = date.today()

                    contracts.append(
                        OptionContract(
                            symbol=symbol,
                            option_type=opt_type,
                            strike=float(opt.get("strike_price", 0)),
                            expiration_date=exp_date,
                            bid=float(opt.get("bid_price", 0)),
                            ask=float(opt.get("ask_price", 0)),
                            mark=float(opt.get("mark_price", 0) or 0),
                            delta=_safe_float(opt.get("delta")),
                            gamma=_safe_float(opt.get("gamma")),
                            theta=_safe_float(opt.get("theta")),
                            vega=_safe_float(opt.get("vega")),
                            iv=_safe_float(opt.get("implied_volatility")),
                            open_interest=int(opt.get("open_interest", 0) or 0),
                            volume=int(opt.get("volume", 0) or 0),
                        )
                    )

        logger.debug(
            "Fetched %d contracts for %s (expirations=%s)",
            len(contracts),
            symbol,
            target_expirations,
        )
        return contracts

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def sell_to_open(
        self,
        symbol: str,
        strike: float,
        expiration: str,
        option_type: str,
        quantity: int,
        price: float,
    ) -> Order:
        """Sell-to-open an option (write a contract)."""
        logger.info(
            "SELL-TO-OPEN %s %s %.2f %s x%d @ %.2f",
            symbol, option_type, strike, expiration, quantity, price,
        )
        try:
            result = rh.orders.order_sell_option_limit(
                positionEffect="open",
                creditOrDebit="credit",
                price=price,
                symbol=symbol,
                quantity=quantity,
                expirationDate=expiration,
                strike=strike,
                optionType=option_type,
                timeInForce="gfd",
            )
            _rate_limit()
        except Exception as exc:
            if _is_throttled(exc):
                logger.warning("Throttled on sell_to_open — retrying in %ds", THROTTLE_RETRY_SECONDS)
                time.sleep(THROTTLE_RETRY_SECONDS)
                return self.sell_to_open(symbol, strike, expiration, option_type, quantity, price)
            raise

        return _parse_order_result(result, symbol, "sell", option_type, strike, expiration, quantity, price)

    def buy_to_close(
        self,
        symbol: str,
        strike: float,
        expiration: str,
        option_type: str,
        quantity: int,
        price: float,
    ) -> Order:
        """Buy-to-close an existing short option."""
        logger.info(
            "BUY-TO-CLOSE %s %s %.2f %s x%d @ %.2f",
            symbol, option_type, strike, expiration, quantity, price,
        )
        try:
            result = rh.orders.order_buy_option_limit(
                positionEffect="close",
                creditOrDebit="debit",
                price=price,
                symbol=symbol,
                quantity=quantity,
                expirationDate=expiration,
                strike=strike,
                optionType=option_type,
                timeInForce="gfd",
            )
            _rate_limit()
        except Exception as exc:
            if _is_throttled(exc):
                logger.warning("Throttled on buy_to_close — retrying in %ds", THROTTLE_RETRY_SECONDS)
                time.sleep(THROTTLE_RETRY_SECONDS)
                return self.buy_to_close(symbol, strike, expiration, option_type, quantity, price)
            raise

        return _parse_order_result(result, symbol, "buy", option_type, strike, expiration, quantity, price)

    def buy_to_open(
        self,
        symbol: str,
        strike: float,
        expiration: str,
        option_type: str,
        quantity: int,
        price: float,
    ) -> Order:
        """Buy-to-open an option (e.g. LEAPS purchase)."""
        logger.info(
            "BUY-TO-OPEN %s %s %.2f %s x%d @ %.2f",
            symbol, option_type, strike, expiration, quantity, price,
        )
        try:
            result = rh.orders.order_buy_option_limit(
                positionEffect="open",
                creditOrDebit="debit",
                price=price,
                symbol=symbol,
                quantity=quantity,
                expirationDate=expiration,
                strike=strike,
                optionType=option_type,
                timeInForce="gfd",
            )
            _rate_limit()
        except Exception as exc:
            if _is_throttled(exc):
                logger.warning("Throttled on buy_to_open — retrying in %ds", THROTTLE_RETRY_SECONDS)
                time.sleep(THROTTLE_RETRY_SECONDS)
                return self.buy_to_open(symbol, strike, expiration, option_type, quantity, price)
            raise

        return _parse_order_result(result, symbol, "buy", option_type, strike, expiration, quantity, price)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def get_order_status(self, order_id: str) -> Order:
        """Check the current status of an order by its ID."""
        try:
            info = rh.orders.get_option_order_info(order_id)
            _rate_limit()
        except Exception as exc:
            if _is_throttled(exc):
                logger.warning("Throttled on get_order_status — retrying in %ds", THROTTLE_RETRY_SECONDS)
                time.sleep(THROTTLE_RETRY_SECONDS)
                return self.get_order_status(order_id)
            raise

        if not info:
            logger.warning("No info returned for order %s", order_id)
            return Order(
                order_id=order_id,
                symbol="UNKNOWN",
                action="unknown",
                option_type="unknown",
                strike=0.0,
                expiration_date=date.today(),
                quantity=0,
                limit_price=0.0,
                status="unknown",
            )

        # Extract leg details from the first leg
        legs = info.get("legs", [{}])
        leg = legs[0] if legs else {}

        fill_price: Optional[float] = None
        if info.get("price"):
            fill_price = float(info["price"])

        fill_date: Optional[datetime] = None
        if info.get("updated_at"):
            try:
                fill_date = datetime.fromisoformat(info["updated_at"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        created_at = datetime.now()
        if info.get("created_at"):
            try:
                created_at = datetime.fromisoformat(info["created_at"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        exp_str = leg.get("expiration_date", "")
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            exp_date = date.today()

        action = "sell" if leg.get("side") == "sell" else "buy"

        return Order(
            order_id=order_id,
            symbol=info.get("chain_symbol", "UNKNOWN"),
            action=action,
            option_type=leg.get("option_type", "unknown"),
            strike=float(leg.get("strike_price", 0)),
            expiration_date=exp_date,
            quantity=int(float(info.get("quantity", 0))),
            limit_price=float(info.get("price", 0)),
            status=info.get("state", "unknown"),
            fill_price=fill_price,
            fill_date=fill_date,
            created_at=created_at,
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Returns True on success."""
        logger.info("Cancelling order %s", order_id)
        try:
            result = rh.orders.cancel_option_order(order_id)
            _rate_limit()
        except Exception as exc:
            if _is_throttled(exc):
                logger.warning("Throttled on cancel_order — retrying in %ds", THROTTLE_RETRY_SECONDS)
                time.sleep(THROTTLE_RETRY_SECONDS)
                return self.cancel_order(order_id)
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

        if result and result != {}:
            logger.info("Order %s cancelled", order_id)
            return True

        logger.warning("Cancel may have failed for order %s — result: %s", order_id, result)
        return False


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _safe_float(value: object) -> Optional[float]:
    """Convert to float, returning None for missing/invalid values."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_order_result(
    result: dict | None,
    symbol: str,
    action: str,
    option_type: str,
    strike: float,
    expiration: str,
    quantity: int,
    price: float,
) -> Order:
    """Convert a robin_stocks order response into an Order dataclass."""
    if not result or "id" not in result:
        logger.error("Order submission failed — result: %s", result)
        try:
            exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            exp_date = date.today()

        return Order(
            order_id="FAILED",
            symbol=symbol,
            action=action,
            option_type=option_type,
            strike=strike,
            expiration_date=exp_date,
            quantity=quantity,
            limit_price=price,
            status="failed",
        )

    try:
        exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        exp_date = date.today()

    created_at = datetime.now()
    if result.get("created_at"):
        try:
            created_at = datetime.fromisoformat(result["created_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    order = Order(
        order_id=result["id"],
        symbol=symbol,
        action=action,
        option_type=option_type,
        strike=strike,
        expiration_date=exp_date,
        quantity=quantity,
        limit_price=price,
        status=result.get("state", "queued"),
        created_at=created_at,
    )
    logger.info("Order placed: %s %s — id=%s status=%s", action, symbol, order.order_id, order.status)
    return order
