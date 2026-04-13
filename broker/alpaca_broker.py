"""Alpaca broker implementation — official API for stocks + options.

Uses alpaca-py SDK for:
- Account info and buying power
- Stock/option positions
- Option chain data
- Order placement and tracking
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    OptionLatestQuoteRequest,
    StockLatestQuoteRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderClass, OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
)

from broker.models import AccountInfo, OptionContract, Order, StockQuote
from utils.logger import get_logger
from utils.timing import now_et

log = get_logger(__name__)


class AlpacaBroker:
    """Alpaca broker wrapper with clean interface for WheelBot."""

    def __init__(self, paper: bool = True) -> None:
        if paper:
            api_key = os.getenv("ALPACA_PAPER_API_KEY", "")
            secret_key = os.getenv("ALPACA_PAPER_SECRET_KEY", "")
        else:
            api_key = os.getenv("ALPACA_API_KEY", "")
            secret_key = os.getenv("ALPACA_SECRET_KEY", "")

        if not api_key or not secret_key:
            raise ValueError("Alpaca API keys not set in .env")

        self.paper = paper
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.stock_data = StockHistoricalDataClient(api_key, secret_key)
        self.option_data = OptionHistoricalDataClient(api_key, secret_key)

        log.info(
            "Alpaca broker initialized (%s mode)",
            "paper" if paper else "LIVE",
        )

    # ── Account ────────────────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        """Get full account info."""
        acct = self.trading.get_account()
        return AccountInfo(
            buying_power=float(acct.buying_power),
            cash_balance=float(acct.cash),
            portfolio_value=float(acct.portfolio_value),
            day_trade_count=acct.daytrade_count or 0,
        )

    def get_buying_power(self) -> float:
        """Get current buying power."""
        acct = self.trading.get_account()
        return float(acct.buying_power)

    # ── Positions ──────────────────────────────────────────────────────────

    def get_stock_positions(self) -> list[dict]:
        """Get all open stock positions."""
        positions = self.trading.get_all_positions()
        stocks = []
        for pos in positions:
            if pos.asset_class == AssetClass.US_EQUITY:
                stocks.append({
                    "symbol": pos.symbol,
                    "quantity": int(float(pos.qty)),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "current_price": float(pos.current_price),
                    "market_value": float(pos.market_value),
                    "unrealized_pl": float(pos.unrealized_pl),
                    "unrealized_plpc": float(pos.unrealized_plpc),
                })
        return stocks

    def get_option_positions(self) -> list[dict]:
        """Get all open option positions."""
        positions = self.trading.get_all_positions()
        options = []
        for pos in positions:
            if pos.asset_class == AssetClass.US_OPTION:
                options.append({
                    "symbol": pos.symbol,
                    "quantity": int(float(pos.qty)),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "current_price": float(pos.current_price),
                    "market_value": float(pos.market_value),
                    "unrealized_pl": float(pos.unrealized_pl),
                    "side": str(pos.side),
                })
        return options

    # ── Market Data ────────────────────────────────────────────────────────

    def get_stock_quote(self, symbol: str) -> Optional[StockQuote]:
        """Get latest quote for a stock."""
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.stock_data.get_stock_latest_quote(req)
            q = quotes.get(symbol)
            if not q:
                return None
            return StockQuote(
                symbol=symbol,
                price=(q.bid_price + q.ask_price) / 2,
                change_pct=0.0,  # Would need previous close for this
                volume=0,
            )
        except Exception as e:
            log.warning("Failed to get quote for %s: %s", symbol, e)
            return None

    def get_option_chain(
        self,
        symbol: str,
        expiration_date: Optional[str] = None,
    ) -> list[OptionContract]:
        """Get options chain for a symbol.

        Returns list of OptionContract with Greeks, bid/ask, etc.
        """
        try:
            # Build the request for option contracts
            params = {
                "underlying_symbols": [symbol],
                "status": "active",
            }

            if expiration_date:
                params["expiration_date"] = expiration_date
            else:
                # Default: get options expiring in next 7-60 days
                today = now_et().date()
                params["expiration_date_gte"] = (today + timedelta(days=7)).isoformat()
                params["expiration_date_lte"] = (today + timedelta(days=60)).isoformat()

            request = GetOptionContractsRequest(**params)
            contracts = self.trading.get_option_contracts(request)

            if not contracts or not contracts.option_contracts:
                log.warning("No option contracts found for %s", symbol)
                return []

            results = []
            # Get quotes for the contracts (batch)
            contract_symbols = [c.symbol for c in contracts.option_contracts[:50]]  # Limit to 50

            try:
                quote_req = OptionLatestQuoteRequest(symbol_or_symbols=contract_symbols)
                quotes = self.option_data.get_option_latest_quote(quote_req)
            except Exception:
                quotes = {}

            for contract in contracts.option_contracts[:50]:
                quote = quotes.get(contract.symbol)
                bid = float(quote.bid_price) if quote and quote.bid_price else 0.0
                ask = float(quote.ask_price) if quote and quote.ask_price else 0.0

                results.append(OptionContract(
                    symbol=contract.symbol,
                    option_type="call" if contract.type == "call" else "put",
                    strike=float(contract.strike_price),
                    expiration_date=contract.expiration_date.isoformat() if contract.expiration_date else "",
                    bid=bid,
                    ask=ask,
                    mark=(bid + ask) / 2 if (bid + ask) > 0 else 0.0,
                    delta=getattr(contract, 'delta', None),
                    gamma=getattr(contract, 'gamma', None),
                    theta=getattr(contract, 'theta', None),
                    vega=getattr(contract, 'vega', None),
                    iv=getattr(contract, 'implied_volatility', None),
                    open_interest=getattr(contract, 'open_interest', 0) or 0,
                    volume=0,
                ))

            log.info("Got %d option contracts for %s", len(results), symbol)
            return results

        except Exception as e:
            log.warning("Failed to get option chain for %s: %s", symbol, e)
            return []

    # ── Orders ─────────────────────────────────────────────────────────────

    def sell_to_open(
        self,
        symbol: str,
        strike: float,
        expiration: str,
        option_type: str,
        quantity: int,
        price: float,
    ) -> Order:
        """Sell to open an option (for CSP, CC, short call)."""
        option_symbol = self._build_option_symbol(symbol, strike, expiration, option_type)
        return self._place_option_order(option_symbol, OrderSide.SELL, quantity, price, "sell_to_open")

    def buy_to_close(
        self,
        symbol: str,
        strike: float,
        expiration: str,
        option_type: str,
        quantity: int,
        price: float,
    ) -> Order:
        """Buy to close an option."""
        option_symbol = self._build_option_symbol(symbol, strike, expiration, option_type)
        return self._place_option_order(option_symbol, OrderSide.BUY, quantity, price, "buy_to_close")

    def buy_to_open(
        self,
        symbol: str,
        strike: float,
        expiration: str,
        option_type: str,
        quantity: int,
        price: float,
    ) -> Order:
        """Buy to open an option (for LEAPS purchase)."""
        option_symbol = self._build_option_symbol(symbol, strike, expiration, option_type)
        return self._place_option_order(option_symbol, OrderSide.BUY, quantity, price, "buy_to_open")

    def get_order_status(self, order_id: str) -> Optional[Order]:
        """Check status of an existing order."""
        try:
            order = self.trading.get_order_by_id(order_id)
            return Order(
                order_id=str(order.id),
                symbol=order.symbol,
                action=str(order.side),
                option_type="",
                strike=0.0,
                expiration_date="",
                quantity=int(float(order.qty)),
                limit_price=float(order.limit_price) if order.limit_price else 0.0,
                status=str(order.status),
                fill_price=float(order.filled_avg_price) if order.filled_avg_price else None,
                fill_date=order.filled_at,
            )
        except Exception as e:
            log.warning("Failed to get order %s: %s", order_id, e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            self.trading.cancel_order_by_id(order_id)
            log.info("Cancelled order %s", order_id)
            return True
        except Exception as e:
            log.warning("Failed to cancel order %s: %s", order_id, e)
            return False

    # ── Spread Orders ──────────────────────────────────────────────────────

    def sell_put_spread(
        self,
        symbol: str,
        short_strike: float,
        long_strike: float,
        expiration: str,
        quantity: int,
        credit: float,
    ) -> Order:
        """Sell a put credit spread (bull put spread) as a single atomic multi-leg order.

        Uses Alpaca's MLEG order class so both legs fill together or not at all.
        This prevents the dangerous state of a naked short put.
        """
        short_symbol = self._build_option_symbol(symbol, short_strike, expiration, "put")
        long_symbol = self._build_option_symbol(symbol, long_strike, expiration, "put")

        try:
            order_legs = [
                OptionLegRequest(
                    symbol=short_symbol,
                    side=OrderSide.SELL,
                    ratio_qty=1,
                ),
                OptionLegRequest(
                    symbol=long_symbol,
                    side=OrderSide.BUY,
                    ratio_qty=1,
                ),
            ]

            request = LimitOrderRequest(
                qty=quantity,
                limit_price=credit,
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                legs=order_legs,
            )

            log.info(
                "Placing MLEG put spread: Sell %s / Buy %s x%d for $%.2f credit",
                short_symbol, long_symbol, quantity, credit,
            )

            order = self.trading.submit_order(order_data=request)

            # Extract individual leg order IDs if available
            long_leg_order_id = None
            if hasattr(order, "legs") and order.legs:
                for leg in order.legs:
                    leg_symbol = getattr(leg, "symbol", "")
                    if leg_symbol == long_symbol:
                        long_leg_order_id = str(leg.id) if hasattr(leg, "id") and leg.id else None
                        break

            log.info(
                "MLEG spread order accepted (ID: %s, long leg: %s)",
                order.id, long_leg_order_id or "shared",
            )

            return Order(
                order_id=str(order.id),
                symbol=f"{symbol} {short_strike}/{long_strike}P",
                action="sell_put_spread",
                option_type="put",
                strike=short_strike,
                expiration_date=expiration,
                quantity=quantity,
                limit_price=credit,
                status=str(order.status),
                secondary_order_id=long_leg_order_id,
            )

        except Exception as e:
            log.error("MLEG spread order failed: %s", e)
            return Order(
                order_id="",
                symbol=f"{symbol} {short_strike}/{long_strike}P",
                action="sell_put_spread",
                option_type="put",
                strike=short_strike,
                expiration_date=expiration,
                quantity=quantity,
                limit_price=credit,
                status="rejected",
            )

    def close_put_spread(
        self,
        symbol: str,
        short_strike: float,
        long_strike: float,
        expiration: str,
        quantity: int,
        debit: float,
    ) -> Order:
        """Close a put credit spread as a single atomic multi-leg order.

        Buys back the short put and sells the long put together via MLEG.
        This ensures the risky short leg is always covered.
        """
        short_symbol = self._build_option_symbol(symbol, short_strike, expiration, "put")
        long_symbol = self._build_option_symbol(symbol, long_strike, expiration, "put")

        try:
            order_legs = [
                OptionLegRequest(
                    symbol=short_symbol,
                    side=OrderSide.BUY,
                    ratio_qty=1,
                ),
                OptionLegRequest(
                    symbol=long_symbol,
                    side=OrderSide.SELL,
                    ratio_qty=1,
                ),
            ]

            request = LimitOrderRequest(
                qty=quantity,
                limit_price=debit,
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                legs=order_legs,
            )

            log.info(
                "Closing MLEG put spread: Buy %s / Sell %s x%d for $%.2f debit",
                short_symbol, long_symbol, quantity, debit,
            )

            order = self.trading.submit_order(order_data=request)

            log.info("MLEG close order accepted (ID: %s)", order.id)

            return Order(
                order_id=str(order.id),
                symbol=f"{symbol} {short_strike}/{long_strike}P",
                action="close_put_spread",
                option_type="put",
                strike=short_strike,
                expiration_date=expiration,
                quantity=quantity,
                limit_price=debit,
                status=str(order.status),
            )

        except Exception as e:
            log.error("MLEG close spread order failed: %s", e)
            return Order(
                order_id="",
                symbol=f"{symbol} {short_strike}/{long_strike}P",
                action="close_put_spread",
                option_type="put",
                strike=short_strike,
                expiration_date=expiration,
                quantity=quantity,
                limit_price=debit,
                status="rejected",
            )

    # ── Market Orders (for stop-losses / urgent exits) ─────────────────────

    def market_close_option(
        self,
        symbol: str,
        strike: float,
        expiration: str,
        option_type: str,
        quantity: int,
    ) -> Order:
        """Close an option position using a market order.

        Used for stop-losses and urgent exits where speed matters more
        than fill price. A market order guarantees execution.
        """
        option_symbol = self._build_option_symbol(symbol, strike, expiration, option_type)
        try:
            request = MarketOrderRequest(
                symbol=option_symbol,
                qty=quantity,
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )

            order = self.trading.submit_order(request)
            log.info(
                "MARKET order placed (urgent): buy_to_close %d x %s (ID: %s)",
                quantity, option_symbol, order.id,
            )

            return Order(
                order_id=str(order.id),
                symbol=option_symbol,
                action="market_buy_to_close",
                option_type=option_type,
                strike=strike,
                expiration_date=expiration,
                quantity=quantity,
                limit_price=0.0,
                status=str(order.status),
            )
        except Exception as e:
            log.error("Market order failed (buy_to_close %s): %s", option_symbol, e)
            return Order(
                order_id="",
                symbol=option_symbol,
                action="market_buy_to_close",
                option_type=option_type,
                strike=strike,
                expiration_date=expiration,
                quantity=quantity,
                limit_price=0.0,
                status="rejected",
            )

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _place_option_order(
        self,
        option_symbol: str,
        side: OrderSide,
        quantity: int,
        price: float,
        action_desc: str,
    ) -> Order:
        """Place a limit order for an option contract."""
        try:
            request = LimitOrderRequest(
                symbol=option_symbol,
                qty=quantity,
                side=side,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=price,
            )

            order = self.trading.submit_order(request)
            log.info(
                "Order placed: %s %d x %s @ $%.2f (ID: %s)",
                action_desc, quantity, option_symbol, price, order.id,
            )

            return Order(
                order_id=str(order.id),
                symbol=option_symbol,
                action=action_desc,
                option_type="",
                strike=0.0,
                expiration_date="",
                quantity=quantity,
                limit_price=price,
                status=str(order.status),
            )
        except Exception as e:
            log.error("Order failed (%s %s): %s", action_desc, option_symbol, e)
            return Order(
                order_id="",
                symbol=option_symbol,
                action=action_desc,
                option_type="",
                strike=0.0,
                expiration_date="",
                quantity=quantity,
                limit_price=price,
                status="rejected",
            )

    @staticmethod
    def _build_option_symbol(
        underlying: str,
        strike: float,
        expiration: str,
        option_type: str,
    ) -> str:
        """Build OCC option symbol format: AAPL240119C00150000.

        Format: SYMBOL + YYMMDD + C/P + strike*1000 (8 digits zero-padded)
        """
        # Parse expiration date
        exp_date = datetime.strptime(expiration, "%Y-%m-%d")
        date_str = exp_date.strftime("%y%m%d")

        # Option type
        type_char = "C" if option_type.lower() == "call" else "P"

        # Strike: multiply by 1000 and zero-pad to 8 digits
        # Use round() instead of int() to prevent floating-point truncation
        # (e.g. $99.9999... * 1000 = 99999.9 would truncate to 99999 with int())
        strike_int = round(strike * 1000)
        strike_str = f"{strike_int:08d}"

        # Pad underlying to 6 chars
        padded = underlying.ljust(6)

        return f"{padded}{date_str}{type_char}{strike_str}"
