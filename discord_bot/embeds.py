"""Rich embed formatters for WheelBot Discord messages."""

from __future__ import annotations

from datetime import datetime

import discord

from data.models import (
    Execution,
    Position,
    Signal,
    SignalAction,
    Strategy,
    Urgency,
)
from utils.timing import now_et

# ── Color palette ─────────────────────────────────────────────────────────

COLOR_ENTRY = discord.Colour.green()       # New trade signals
COLOR_EXIT = discord.Colour.gold()         # Exit / close
COLOR_ROLL = discord.Colour.blue()         # Roll recommendation
COLOR_FILL = discord.Colour.dark_green()   # Order filled
COLOR_PORTFOLIO = discord.Colour.teal()    # Portfolio overview
COLOR_PERFORMANCE = discord.Colour.purple()
COLOR_ERROR = discord.Colour.red()
COLOR_WARNING = discord.Colour.yellow()
COLOR_INFO = discord.Colour.light_grey()

# ── Formatting helpers ────────────────────────────────────────────────────


def _money(value: float | None) -> str:
    """Format a dollar amount as $X,XXX.XX."""
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _pct(value: float | None) -> str:
    """Format a percentage as XX.X%."""
    if value is None:
        return "N/A"
    return f"{value:+.1f}%"


def _date_dte(expiration: str | None) -> str:
    """Format expiration as 'Apr 25, 2026 (15 DTE)'."""
    if not expiration:
        return "N/A"
    try:
        exp_dt = datetime.strptime(expiration, "%Y-%m-%d")
        dte = (exp_dt.date() - now_et().date()).days
        formatted = exp_dt.strftime("%b %d, %Y")
        return f"{formatted} ({dte} DTE)"
    except (ValueError, TypeError):
        return expiration


def _urgency_badge(urgency: str) -> str:
    """Return an urgency indicator string."""
    badges = {
        Urgency.URGENT.value: "🔴 URGENT",
        Urgency.NORMAL.value: "🟢 Normal",
        Urgency.INFO.value: "ℹ️ Info",
    }
    return badges.get(urgency, urgency)


def _action_label(action: str) -> str:
    """Human-readable action label."""
    labels = {
        SignalAction.BUY_LEAPS.value: "Buy LEAPS Call",
        SignalAction.SELL_SHORT_CALL.value: "Sell Short Call",
        SignalAction.SELL_CSP.value: "Sell Cash-Secured Put",
        SignalAction.SELL_CC.value: "Sell Covered Call",
        SignalAction.BUY_TO_CLOSE.value: "Buy to Close",
        SignalAction.ROLL.value: "Roll Position",
        SignalAction.CLOSE_PAIR.value: "Close Pair",
    }
    return labels.get(action, action)


def _strategy_label(strategy: str) -> str:
    """Human-readable strategy label."""
    labels = {
        Strategy.PMCC_LEAPS.value: "PMCC — LEAPS",
        Strategy.PMCC_SHORT_CALL.value: "PMCC — Short Call",
        Strategy.WHEEL_CSP.value: "Wheel — CSP",
        Strategy.WHEEL_CC.value: "Wheel — Covered Call",
        Strategy.WHEEL_SHARES.value: "Wheel — Shares",
    }
    return labels.get(strategy, strategy)


# ── Embed builders ────────────────────────────────────────────────────────


def signal_embed(signal: Signal) -> discord.Embed:
    """Build an embed for a new trade signal (entry)."""
    title = f"📡 Signal: {_action_label(signal.action)} — {signal.symbol}"
    embed = discord.Embed(title=title, color=COLOR_ENTRY)

    embed.add_field(name="Action", value=_action_label(signal.action), inline=True)
    embed.add_field(name="Symbol", value=signal.symbol, inline=True)
    embed.add_field(name="Strategy", value=_strategy_label(signal.strategy), inline=True)

    if signal.strike is not None:
        embed.add_field(name="Strike", value=f"${signal.strike:.2f}", inline=True)
    if signal.option_type:
        embed.add_field(name="Type", value=signal.option_type.upper(), inline=True)
    embed.add_field(name="Expiration", value=_date_dte(signal.expiration_date), inline=True)

    if signal.limit_price is not None:
        embed.add_field(name="Limit Price", value=_money(signal.limit_price), inline=True)
    if signal.estimated_credit is not None:
        label = "Est. Credit" if signal.estimated_credit >= 0 else "Est. Debit"
        embed.add_field(name=label, value=_money(signal.estimated_credit), inline=True)

    embed.add_field(name="Reason", value=signal.reason or "—", inline=False)

    if signal.ai_analysis:
        analysis = signal.ai_analysis[:1024]  # Embed field limit
        embed.add_field(name="AI Analysis", value=analysis, inline=False)

    if signal.optimal_execution_window:
        embed.add_field(
            name="Optimal Window",
            value=signal.optimal_execution_window,
            inline=True,
        )

    embed.add_field(name="Urgency", value=_urgency_badge(signal.urgency), inline=True)
    embed.set_footer(text=f"Signal #{signal.id} • {now_et().strftime('%I:%M %p ET')}")
    return embed


def exit_embed(signal: Signal, position: Position) -> discord.Embed:
    """Build an embed for an exit signal."""
    title = f"🚪 Exit: {signal.symbol}"
    embed = discord.Embed(title=title, color=COLOR_EXIT)

    embed.add_field(name="Symbol", value=signal.symbol, inline=True)
    embed.add_field(name="Strategy", value=_strategy_label(position.strategy), inline=True)
    embed.add_field(name="Action", value=_action_label(signal.action), inline=True)

    embed.add_field(name="Entry Price", value=_money(position.entry_price), inline=True)
    if signal.limit_price is not None:
        embed.add_field(name="Exit Price", value=_money(signal.limit_price), inline=True)

    if position.pnl_dollars is not None:
        embed.add_field(name="P&L ($)", value=_money(position.pnl_dollars), inline=True)
    if position.pnl_percent is not None:
        embed.add_field(name="P&L (%)", value=_pct(position.pnl_percent), inline=True)

    # Days held
    if position.entry_date:
        try:
            entry_dt = datetime.strptime(position.entry_date[:10], "%Y-%m-%d")
            days_held = (now_et().date() - entry_dt.date()).days
            embed.add_field(name="Days Held", value=str(days_held), inline=True)
        except (ValueError, TypeError):
            pass

    embed.add_field(name="Exit Reason", value=signal.reason or "—", inline=False)
    embed.add_field(
        name="Cumulative Premium",
        value=_money(position.total_premium_collected),
        inline=True,
    )

    embed.set_footer(text=f"Signal #{signal.id} • {now_et().strftime('%I:%M %p ET')}")
    return embed


def roll_embed(signal: Signal, position: Position) -> discord.Embed:
    """Build an embed for a roll recommendation."""
    title = f"🔄 Roll: {signal.symbol}"
    embed = discord.Embed(title=title, color=COLOR_ROLL)

    # Current position
    embed.add_field(name="Symbol", value=position.symbol, inline=True)
    embed.add_field(name="Strategy", value=_strategy_label(position.strategy), inline=True)
    embed.add_field(
        name="Current Strike",
        value=f"${position.strike:.2f}" if position.strike else "N/A",
        inline=True,
    )
    embed.add_field(
        name="Current Expiration",
        value=_date_dte(position.expiration_date),
        inline=True,
    )

    # Recommended new position
    if signal.strike is not None:
        embed.add_field(name="New Strike", value=f"${signal.strike:.2f}", inline=True)
    embed.add_field(name="New Expiration", value=_date_dte(signal.expiration_date), inline=True)

    if signal.estimated_credit is not None:
        label = "Net Credit" if signal.estimated_credit >= 0 else "Net Debit"
        embed.add_field(name=label, value=_money(signal.estimated_credit), inline=True)

    embed.add_field(name="Reason", value=signal.reason or "—", inline=False)

    if signal.ai_analysis:
        embed.add_field(
            name="AI Analysis",
            value=signal.ai_analysis[:1024],
            inline=False,
        )

    embed.set_footer(text=f"Signal #{signal.id} • {now_et().strftime('%I:%M %p ET')}")
    return embed


def fill_embed(execution: Execution, position: Position) -> discord.Embed:
    """Build an embed for a filled order."""
    title = f"✅ Order Filled: {position.symbol}"
    embed = discord.Embed(title=title, color=COLOR_FILL)

    embed.add_field(name="Symbol", value=position.symbol, inline=True)
    embed.add_field(name="Strategy", value=_strategy_label(position.strategy), inline=True)
    embed.add_field(name="Order Type", value=execution.order_type.upper(), inline=True)

    embed.add_field(name="Fill Price", value=_money(execution.fill_price), inline=True)
    if execution.requested_price is not None:
        embed.add_field(
            name="Requested Price",
            value=_money(execution.requested_price),
            inline=True,
        )

    if execution.slippage is not None:
        slip_str = _money(execution.slippage)
        embed.add_field(name="Slippage", value=slip_str, inline=True)

    embed.add_field(name="Status", value="Position Active", inline=False)

    embed.set_footer(
        text=f"Execution #{execution.id} • {now_et().strftime('%I:%M %p ET')}",
    )
    return embed


def portfolio_embed(positions: list[Position]) -> discord.Embed:
    """Build an embed showing all open positions."""
    title = f"📊 Portfolio — {len(positions)} Open Position{'s' if len(positions) != 1 else ''}"
    embed = discord.Embed(title=title, color=COLOR_PORTFOLIO)

    if not positions:
        embed.description = "No open positions."
        return embed

    for pos in positions[:25]:  # Discord embed limit: 25 fields
        dte_str = f"{pos.dte_remaining} DTE" if pos.dte_remaining is not None else ""
        strike_str = f"${pos.strike:.2f}" if pos.strike else ""
        pnl_str = _money(pos.pnl_dollars) if pos.pnl_dollars is not None else ""
        pnl_pct_str = _pct(pos.pnl_percent) if pos.pnl_percent is not None else ""

        value_lines = [
            f"**Strategy:** {_strategy_label(pos.strategy)}",
            f"**Strike:** {strike_str}" if strike_str else None,
            f"**DTE:** {dte_str}" if dte_str else None,
            f"**P&L:** {pnl_str} ({pnl_pct_str})" if pnl_str else None,
            f"**State:** {pos.state}",
        ]
        value = "\n".join(line for line in value_lines if line)
        embed.add_field(name=pos.symbol, value=value, inline=True)

    embed.set_footer(text=now_et().strftime("%I:%M %p ET • %b %d, %Y"))
    return embed


def performance_embed(perf_data: dict) -> discord.Embed:
    """Build an embed showing performance statistics.

    Accepts a dict with keys matching Performance dataclass fields:
    win_rate, avg_profit, total_trades, sharpe_ratio, max_drawdown,
    total_premium_collected, winning_trades, losing_trades, max_win, max_loss.
    """
    title = "📈 Performance Summary"
    embed = discord.Embed(title=title, color=COLOR_PERFORMANCE)

    win_rate = perf_data.get("win_rate", 0)
    embed.add_field(name="Win Rate", value=f"{win_rate:.1f}%", inline=True)
    embed.add_field(
        name="Avg Profit",
        value=_money(perf_data.get("avg_profit")),
        inline=True,
    )
    embed.add_field(
        name="Total Trades",
        value=str(perf_data.get("total_trades", 0)),
        inline=True,
    )

    embed.add_field(
        name="Winning / Losing",
        value=f"{perf_data.get('winning_trades', 0)} / {perf_data.get('losing_trades', 0)}",
        inline=True,
    )
    embed.add_field(
        name="Sharpe Ratio",
        value=f"{perf_data.get('sharpe_ratio', 0):.2f}",
        inline=True,
    )
    embed.add_field(
        name="Max Drawdown",
        value=_pct(perf_data.get("max_drawdown")),
        inline=True,
    )

    embed.add_field(
        name="Max Win",
        value=_money(perf_data.get("max_win")),
        inline=True,
    )
    embed.add_field(
        name="Max Loss",
        value=_money(perf_data.get("max_loss")),
        inline=True,
    )
    embed.add_field(
        name="Total Premium Collected",
        value=_money(perf_data.get("total_premium_collected")),
        inline=False,
    )

    embed.set_footer(text=now_et().strftime("%I:%M %p ET • %b %d, %Y"))
    return embed


def alert_embed(title: str, message: str, level: str = "warning") -> discord.Embed:
    """Generic alert embed. Level: 'error', 'warning', or 'info'."""
    colors = {
        "error": COLOR_ERROR,
        "warning": COLOR_WARNING,
        "info": COLOR_INFO,
    }
    icons = {
        "error": "🔴",
        "warning": "🟡",
        "info": "ℹ️",
    }
    color = colors.get(level, COLOR_INFO)
    icon = icons.get(level, "ℹ️")

    embed = discord.Embed(
        title=f"{icon} {title}",
        description=message,
        color=color,
    )
    embed.set_footer(text=now_et().strftime("%I:%M %p ET • %b %d, %Y"))
    return embed
