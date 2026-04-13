"""Broker-agnostic data types for WheelBot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class AccountInfo:
    """Snapshot of account-level financials."""

    buying_power: float
    cash_balance: float
    portfolio_value: float
    day_trade_count: int


@dataclass
class OptionContract:
    """A single option contract from a chain lookup."""

    symbol: str
    option_type: str  # "call" or "put"
    strike: float
    expiration_date: date
    bid: float
    ask: float
    mark: float
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    iv: Optional[float] = None
    open_interest: int = 0
    volume: int = 0


@dataclass
class StockQuote:
    """Real-time equity quote."""

    symbol: str
    price: float
    change_pct: float
    volume: int


@dataclass
class StockPosition:
    """A currently-held equity position."""

    symbol: str
    quantity: float
    average_buy_price: float
    current_price: float
    equity: float
    percent_change: float


@dataclass
class Position:
    """A currently-held option position."""

    symbol: str
    option_type: str  # "call" or "put"
    strike: float
    expiration_date: date
    quantity: float
    average_price: float
    current_price: float
    option_id: str = ""


@dataclass
class Order:
    """An order submitted to the broker."""

    order_id: str
    symbol: str
    action: str  # "buy" or "sell"
    option_type: str  # "call" or "put"
    strike: float
    expiration_date: date
    quantity: int
    limit_price: float
    status: str = "pending"
    fill_price: Optional[float] = None
    fill_date: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    secondary_order_id: Optional[str] = None  # second leg of a spread
