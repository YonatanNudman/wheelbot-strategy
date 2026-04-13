"""Data models for WheelBot — all table schemas as dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ── Enums ──────────────────────────────────────────────────────────────────

class Strategy(str, Enum):
    PMCC_LEAPS = "pmcc_leaps"
    PMCC_SHORT_CALL = "pmcc_short_call"
    WHEEL_CSP = "wheel_csp"
    WHEEL_CC = "wheel_cc"
    WHEEL_SHARES = "wheel_shares"


class PositionState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ASSIGNED = "assigned"
    EXPIRED = "expired"
    ROLLED = "rolled"


class SignalAction(str, Enum):
    BUY_LEAPS = "buy_leaps"
    SELL_SHORT_CALL = "sell_short_call"
    SELL_CSP = "sell_csp"
    SELL_CC = "sell_cc"
    BUY_TO_CLOSE = "buy_to_close"
    ROLL = "roll"
    CLOSE_PAIR = "close_pair"


class SignalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXECUTED = "executed"
    EXPIRED = "expired"
    AUTO_EXECUTED = "auto_executed"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Urgency(str, Enum):
    NORMAL = "normal"
    URGENT = "urgent"
    INFO = "info"


# ── Position ───────────────────────────────────────────────────────────────

@dataclass
class Position:
    id: Optional[int] = None
    symbol: str = ""
    strategy: str = ""
    pair_id: Optional[str] = None
    state: str = PositionState.OPEN.value

    # What you're holding
    option_type: Optional[str] = None  # 'put' or 'call'
    strike: Optional[float] = None
    expiration_date: Optional[str] = None  # YYYY-MM-DD
    quantity: int = 1

    # Entry details
    entry_date: str = ""
    entry_price: float = 0.0
    entry_credit_total: Optional[float] = None

    # Current state (updated every 15 min)
    current_price: Optional[float] = None
    current_delta: Optional[float] = None
    current_theta: Optional[float] = None
    current_iv: Optional[float] = None
    dte_remaining: Optional[int] = None
    pnl_dollars: Optional[float] = None
    pnl_percent: Optional[float] = None

    # Exit targets (pre-calculated)
    target_close_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    roll_by_date: Optional[str] = None
    next_earnings_date: Optional[str] = None

    # Exit details
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_credit_total: Optional[float] = None

    # Cost basis tracking
    cost_basis: Optional[float] = None
    total_premium_collected: float = 0.0

    # Metadata
    ai_reasoning: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ── Signal ─────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    id: Optional[int] = None
    symbol: str = ""
    strategy: str = ""
    action: str = ""

    # Exact trade details
    option_type: Optional[str] = None
    strike: Optional[float] = None
    expiration_date: Optional[str] = None
    limit_price: Optional[float] = None
    estimated_credit: Optional[float] = None
    estimated_pnl: Optional[float] = None

    # Context
    reason: str = ""
    ai_analysis: Optional[str] = None
    urgency: str = Urgency.NORMAL.value

    # Approval flow
    status: str = SignalStatus.PENDING.value
    discord_message_id: Optional[str] = None
    approved_at: Optional[str] = None
    executed_at: Optional[str] = None

    # Timing
    valid_until: Optional[str] = None
    optimal_execution_window: Optional[str] = None
    created_at: Optional[str] = None


# ── Execution ──────────────────────────────────────────────────────────────

@dataclass
class Execution:
    id: Optional[int] = None
    signal_id: Optional[int] = None
    position_id: Optional[int] = None
    robinhood_order_id: Optional[str] = None
    secondary_order_id: Optional[str] = None  # Second leg of a spread
    order_type: str = "limit"
    requested_price: Optional[float] = None
    fill_price: Optional[float] = None
    fill_date: Optional[str] = None
    slippage: Optional[float] = None
    status: str = OrderStatus.PENDING.value
    error_message: Optional[str] = None
    created_at: Optional[str] = None


# ── Portfolio Snapshot ─────────────────────────────────────────────────────

@dataclass
class PortfolioSnapshot:
    id: Optional[int] = None
    date: str = ""
    total_account_value: float = 0.0
    cash_balance: float = 0.0
    positions_value: float = 0.0
    open_position_count: int = 0
    day_pnl: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    notes: Optional[str] = None
    created_at: Optional[str] = None


# ── Performance ────────────────────────────────────────────────────────────

@dataclass
class Performance:
    id: Optional[int] = None
    strategy: str = "overall"
    period: str = "all_time"
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_profit: float = 0.0
    max_win: float = 0.0
    max_loss: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    total_premium_collected: float = 0.0
    updated_at: Optional[str] = None


# ── Wheel Cycle ───────────────────────────────────────────────────────────

@dataclass
class WheelCycle:
    id: Optional[int] = None
    symbol: str = ""
    state: str = "scanning"
    csp_position_id: Optional[int] = None
    shares_position_id: Optional[int] = None
    cc_position_id: Optional[int] = None
    total_premium_collected: float = 0.0
    cost_basis: Optional[float] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    final_pnl: Optional[float] = None
    notes: Optional[str] = None
