"""SQLite database layer — schema creation, CRUD operations."""

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from data.models import (
    Execution,
    OrderStatus,
    Performance,
    PortfolioSnapshot,
    Position,
    PositionState,
    Signal,
    SignalStatus,
    WheelCycle,
)
from utils.logger import get_logger
from utils.timing import now_et

log = get_logger(__name__)

DB_PATH = Path(__file__).parent.parent / "wheelbot.db"

# ── Schema ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    pair_id TEXT,
    state TEXT NOT NULL DEFAULT 'open',
    option_type TEXT,
    strike REAL,
    expiration_date TEXT,
    quantity INTEGER DEFAULT 1,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_credit_total REAL,
    current_price REAL,
    current_delta REAL,
    current_theta REAL,
    current_iv REAL,
    dte_remaining INTEGER,
    pnl_dollars REAL,
    pnl_percent REAL,
    target_close_price REAL,
    stop_loss_price REAL,
    roll_by_date TEXT,
    next_earnings_date TEXT,
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    exit_credit_total REAL,
    cost_basis REAL,
    total_premium_collected REAL DEFAULT 0.0,
    ai_reasoning TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    action TEXT NOT NULL,
    option_type TEXT,
    strike REAL,
    expiration_date TEXT,
    limit_price REAL,
    estimated_credit REAL,
    estimated_pnl REAL,
    reason TEXT,
    ai_analysis TEXT,
    urgency TEXT DEFAULT 'normal',
    status TEXT DEFAULT 'pending',
    discord_message_id TEXT,
    approved_at TIMESTAMP,
    executed_at TIMESTAMP,
    valid_until TEXT,
    optimal_execution_window TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    position_id INTEGER REFERENCES positions(id),
    robinhood_order_id TEXT,
    secondary_order_id TEXT,
    order_type TEXT DEFAULT 'limit',
    requested_price REAL,
    fill_price REAL,
    fill_date TIMESTAMP,
    slippage REAL,
    status TEXT DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    total_account_value REAL,
    cash_balance REAL,
    positions_value REAL,
    open_position_count INTEGER,
    day_pnl REAL,
    total_pnl REAL,
    total_pnl_pct REAL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wheel_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'scanning',
    csp_position_id INTEGER,
    shares_position_id INTEGER,
    cc_position_id INTEGER,
    total_premium_collected REAL DEFAULT 0.0,
    cost_basis REAL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    final_pnl REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    period TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0.0,
    avg_profit REAL DEFAULT 0.0,
    max_win REAL DEFAULT 0.0,
    max_loss REAL DEFAULT 0.0,
    sharpe_ratio REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    total_premium_collected REAL DEFAULT 0.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


# ── Connection ─────────────────────────────────────────────────────────────

BACKUP_DIR = DB_PATH.parent / "backups"
MAX_BACKUPS = 7


def init_db() -> None:
    """Create all tables if they don't exist. Enables WAL mode for concurrency."""
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    log.info("Database initialized at %s (WAL mode)", DB_PATH)


def backup_db() -> Path | None:
    """Create a daily backup of the SQLite database.

    Copies wheelbot.db to backups/wheelbot_YYYYMMDD.bak.
    Keeps at most MAX_BACKUPS files, deleting older ones.
    Returns the backup path on success, or None on failure.
    """
    if not DB_PATH.exists():
        log.warning("Cannot backup — database file does not exist: %s", DB_PATH)
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    today_str = datetime.now().strftime("%Y%m%d")
    backup_path = BACKUP_DIR / f"wheelbot_{today_str}.bak"

    try:
        shutil.copy2(DB_PATH, backup_path)
        log.info("Database backed up to %s", backup_path)
    except Exception as exc:
        log.error("Database backup failed: %s", exc)
        return None

    # Prune old backups — keep only the most recent MAX_BACKUPS files
    existing = sorted(BACKUP_DIR.glob("wheelbot_*.bak"), reverse=True)
    for old_backup in existing[MAX_BACKUPS:]:
        try:
            old_backup.unlink()
            log.info("Deleted old backup: %s", old_backup.name)
        except OSError as exc:
            log.warning("Failed to delete old backup %s: %s", old_backup.name, exc)

    return backup_path


@contextmanager
def _connect():
    """Context manager for SQLite connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Positions CRUD ─────────────────────────────────────────────────────────

def create_position(pos: Position) -> int:
    """Insert a new position and return its ID."""
    now = now_et().isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO positions (
                symbol, strategy, pair_id, state, option_type, strike,
                expiration_date, quantity, entry_date, entry_price,
                entry_credit_total, target_close_price, stop_loss_price,
                roll_by_date, next_earnings_date, cost_basis,
                total_premium_collected, ai_reasoning, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pos.symbol, pos.strategy, pos.pair_id, pos.state, pos.option_type,
                pos.strike, pos.expiration_date, pos.quantity, pos.entry_date,
                pos.entry_price, pos.entry_credit_total, pos.target_close_price,
                pos.stop_loss_price, pos.roll_by_date, pos.next_earnings_date,
                pos.cost_basis, pos.total_premium_collected, pos.ai_reasoning,
                pos.notes, now, now,
            ),
        )
        return cursor.lastrowid


def get_open_positions(strategy: Optional[str] = None, symbol: Optional[str] = None) -> list[Position]:
    """Get all open positions, optionally filtered by strategy and/or symbol."""
    with _connect() as conn:
        clauses = ["state = 'open'"]
        params = []
        if strategy:
            clauses.append("strategy LIKE ?")
            params.append(f"{strategy}%")
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = " AND ".join(clauses)
        rows = conn.execute(f"SELECT * FROM positions WHERE {where}", params).fetchall()
    return [_row_to_position(r) for r in rows]


def get_position(position_id: int) -> Optional[Position]:
    """Get a single position by ID."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
    return _row_to_position(row) if row else None


POSITION_COLUMNS = {
    'state', 'current_price', 'current_delta', 'current_theta', 'current_iv',
    'dte_remaining', 'pnl_dollars', 'pnl_percent', 'target_close_price',
    'stop_loss_price', 'roll_by_date', 'next_earnings_date', 'exit_date',
    'exit_price', 'exit_reason', 'exit_credit_total', 'cost_basis',
    'total_premium_collected', 'ai_reasoning', 'notes', 'updated_at',
    'pair_id', 'option_type', 'strike', 'expiration_date', 'quantity',
    'entry_date', 'entry_price', 'entry_credit_total', 'symbol', 'strategy',
}

EXECUTION_COLUMNS = {
    'status', 'fill_price', 'fill_date', 'slippage', 'error_message',
    'position_id', 'robinhood_order_id', 'secondary_order_id', 'order_type',
    'requested_price', 'signal_id',
}

SIGNAL_COLUMNS = {
    'status', 'discord_message_id', 'approved_at', 'executed_at',
    'valid_until', 'urgency', 'reason', 'ai_analysis',
    'limit_price', 'estimated_credit', 'estimated_pnl',
}


def update_position(position_id: int, **kwargs) -> None:
    """Update specific fields on a position.

    Validates column names against a whitelist to prevent SQL injection.
    """
    kwargs["updated_at"] = now_et().isoformat()
    for k in kwargs:
        if k not in POSITION_COLUMNS:
            raise ValueError(f"Invalid position column: {k}")
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [position_id]
    with _connect() as conn:
        conn.execute(
            f"UPDATE positions SET {set_clause} WHERE id = ?", values
        )


def close_position(position_id: int, exit_price: float, exit_reason: str) -> None:
    """Close a position with exit details and calculate P&L."""
    pos = get_position(position_id)
    if not pos:
        return

    # Options use 100 multiplier (1 contract = 100 shares)
    # Share positions use quantity directly (quantity IS the number of shares)
    if pos.strategy == 'wheel_shares':
        multiplier = 1  # quantity is already in shares
    else:
        multiplier = 100  # options: 1 contract = 100 shares

    pnl = (pos.entry_price - exit_price) * pos.quantity * multiplier
    pnl_pct = (pnl / (pos.entry_price * pos.quantity * multiplier)) * 100 if pos.entry_price else 0

    update_position(
        position_id,
        state=PositionState.CLOSED.value,
        exit_date=now_et().strftime("%Y-%m-%d"),
        exit_price=exit_price,
        exit_reason=exit_reason,
        exit_credit_total=exit_price * pos.quantity * multiplier,
        pnl_dollars=pnl,
        pnl_percent=pnl_pct,
    )


def get_positions_by_pair(pair_id: str) -> list[Position]:
    """Get all positions linked by a PMCC pair ID."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE pair_id = ?", (pair_id,)
        ).fetchall()
    return [_row_to_position(r) for r in rows]


def count_open_positions(strategy: Optional[str] = None) -> int:
    """Count currently open positions."""
    with _connect() as conn:
        if strategy:
            row = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE state = 'open' AND strategy LIKE ?",
                (f"{strategy}%",),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE state = 'open'"
            ).fetchone()
    return row[0]


# ── Signals CRUD ───────────────────────────────────────────────────────────

def create_signal(sig: Signal) -> int:
    """Insert a new signal and return its ID."""
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO signals (
                symbol, strategy, action, option_type, strike, expiration_date,
                limit_price, estimated_credit, estimated_pnl, reason,
                ai_analysis, urgency, status, valid_until,
                optimal_execution_window
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sig.symbol, sig.strategy, sig.action, sig.option_type,
                sig.strike, sig.expiration_date, sig.limit_price,
                sig.estimated_credit, sig.estimated_pnl, sig.reason,
                sig.ai_analysis, sig.urgency, sig.status,
                sig.valid_until, sig.optimal_execution_window,
            ),
        )
        return cursor.lastrowid


def get_pending_signals() -> list[Signal]:
    """Get all signals awaiting approval."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_signal(r) for r in rows]


def update_signal(signal_id: int, **kwargs) -> None:
    """Update specific fields on a signal.

    Validates column names against a whitelist to prevent SQL injection.
    """
    for k in kwargs:
        if k not in SIGNAL_COLUMNS:
            raise ValueError(f"Invalid signal column: {k}")
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [signal_id]
    with _connect() as conn:
        conn.execute(f"UPDATE signals SET {set_clause} WHERE id = ?", values)


# ── Executions CRUD ────────────────────────────────────────────────────────

def create_execution(exe: Execution) -> int:
    """Insert a new execution record and return its ID."""
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO executions (
                signal_id, position_id, robinhood_order_id, secondary_order_id,
                order_type, requested_price, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                exe.signal_id, exe.position_id, exe.robinhood_order_id,
                exe.secondary_order_id, exe.order_type, exe.requested_price,
                exe.status,
            ),
        )
        return cursor.lastrowid


def get_pending_executions() -> list[Execution]:
    """Get all executions that haven't filled yet."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM executions WHERE status = 'pending'"
        ).fetchall()
    return [_row_to_execution(r) for r in rows]


def get_last_fill_date() -> Optional[datetime]:
    """Return the fill_date of the most recent successful execution, or None.

    Used by the silent-failure alarm to detect prolonged no-trade periods.
    Only considers executions with a non-null fill_date — i.e., actually filled,
    not just requested.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT fill_date FROM executions "
            "WHERE fill_date IS NOT NULL "
            "ORDER BY fill_date DESC LIMIT 1"
        ).fetchone()
    if not row or not row["fill_date"]:
        return None
    # SQLite stores timestamps as strings; parse back to datetime
    raw = row["fill_date"]
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def update_execution(execution_id: int, **kwargs) -> None:
    """Update specific fields on an execution.

    Validates column names against a whitelist to prevent SQL injection.
    """
    for k in kwargs:
        if k not in EXECUTION_COLUMNS:
            raise ValueError(f"Invalid execution column: {k}")
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [execution_id]
    with _connect() as conn:
        conn.execute(f"UPDATE executions SET {set_clause} WHERE id = ?", values)


# ── Portfolio Snapshots ────────────────────────────────────────────────────

def save_snapshot(snap: PortfolioSnapshot) -> int:
    """Insert or update a daily portfolio snapshot."""
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT OR REPLACE INTO portfolio_snapshots (
                date, total_account_value, cash_balance, positions_value,
                open_position_count, day_pnl, total_pnl, total_pnl_pct, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snap.date, snap.total_account_value, snap.cash_balance,
                snap.positions_value, snap.open_position_count,
                snap.day_pnl, snap.total_pnl, snap.total_pnl_pct, snap.notes,
            ),
        )
        return cursor.lastrowid


def get_latest_snapshot() -> Optional[PortfolioSnapshot]:
    """Get the most recent portfolio snapshot."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
    return _row_to_snapshot(row) if row else None


def get_first_snapshot() -> Optional[PortfolioSnapshot]:
    """Get the first-ever portfolio snapshot (for all-time P&L)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY date ASC LIMIT 1"
        ).fetchone()
    return _row_to_snapshot(row) if row else None


# ── Performance ────────────────────────────────────────────────────────────

def save_performance(perf: Performance) -> None:
    """Insert or update performance stats for a strategy/period combo."""
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO performance (
                strategy, period, total_trades, winning_trades, losing_trades,
                win_rate, avg_profit, max_win, max_loss, sharpe_ratio,
                max_drawdown, total_premium_collected, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                perf.strategy, perf.period, perf.total_trades,
                perf.winning_trades, perf.losing_trades, perf.win_rate,
                perf.avg_profit, perf.max_win, perf.max_loss,
                perf.sharpe_ratio, perf.max_drawdown,
                perf.total_premium_collected, now_et().isoformat(),
            ),
        )


def get_performance(strategy: str = "overall", period: str = "all_time") -> Optional[Performance]:
    """Get performance stats for a strategy/period."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM performance WHERE strategy = ? AND period = ?",
            (strategy, period),
        ).fetchone()
    return _row_to_performance(row) if row else None


def get_closed_trades(strategy: Optional[str] = None) -> list[Position]:
    """Get all closed positions for performance calculation."""
    with _connect() as conn:
        if strategy:
            rows = conn.execute(
                "SELECT * FROM positions WHERE state = 'closed' AND strategy LIKE ? ORDER BY exit_date DESC",
                (f"{strategy}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions WHERE state = 'closed' ORDER BY exit_date DESC"
            ).fetchall()
    return [_row_to_position(r) for r in rows]


# ── Row Converters ─────────────────────────────────────────────────────────

def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(**{k: row[k] for k in row.keys()})


def _row_to_signal(row: sqlite3.Row) -> Signal:
    return Signal(**{k: row[k] for k in row.keys()})


def _row_to_execution(row: sqlite3.Row) -> Execution:
    return Execution(**{k: row[k] for k in row.keys()})


def _row_to_snapshot(row: sqlite3.Row) -> PortfolioSnapshot:
    return PortfolioSnapshot(**{k: row[k] for k in row.keys()})


def _row_to_performance(row: sqlite3.Row) -> Performance:
    return Performance(**{k: row[k] for k in row.keys()})


def _row_to_wheel_cycle(row: sqlite3.Row) -> WheelCycle:
    return WheelCycle(**{k: row[k] for k in row.keys()})


# ── Wheel Cycle CRUD ──────────────────────────────────────────────────────

WHEEL_CYCLE_COLUMNS = {
    'symbol', 'state', 'csp_position_id', 'shares_position_id',
    'cc_position_id', 'total_premium_collected', 'cost_basis',
    'started_at', 'completed_at', 'final_pnl', 'notes',
}


def create_wheel_cycle(symbol: str) -> int:
    """Create a new wheel cycle for *symbol* and return its ID."""
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO wheel_cycles (symbol, state, started_at) VALUES (?, 'scanning', ?)",
            (symbol, now_et().isoformat()),
        )
        return cursor.lastrowid


def get_active_wheel_cycle(symbol: str) -> Optional[WheelCycle]:
    """Get the active (non-completed) wheel cycle for *symbol*, if any."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM wheel_cycles WHERE symbol = ? AND completed_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    return _row_to_wheel_cycle(row) if row else None


def update_wheel_cycle(cycle_id: int, **kwargs) -> None:
    """Update specific fields on a wheel cycle."""
    for k in kwargs:
        if k not in WHEEL_CYCLE_COLUMNS:
            raise ValueError(f"Invalid wheel_cycle column: {k}")
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [cycle_id]
    with _connect() as conn:
        conn.execute(f"UPDATE wheel_cycles SET {set_clause} WHERE id = ?", values)


def complete_wheel_cycle(cycle_id: int, final_pnl: float) -> None:
    """Mark a wheel cycle as completed with its final P&L."""
    with _connect() as conn:
        conn.execute(
            "UPDATE wheel_cycles SET state = 'completed', completed_at = ?, final_pnl = ? WHERE id = ?",
            (now_et().isoformat(), final_pnl, cycle_id),
        )
