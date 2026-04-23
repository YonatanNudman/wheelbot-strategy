"""Daily Reflection + Weekly Autopsy — AI-written retrospectives.

These were previously Claude Code cloud routines, but those sit inside an
Anthropic egress proxy that blocks `api.alpaca.markets`. Moving the jobs here
(Railway, no egress restrictions) and using OpenAI for the analysis gets us
fully autonomous retrospective writing without losing the Morning-Scan feedback
loop (scan reads `reflections/` looking for "remove" flags on bad stocks).

Pure parts here (prompt building + file writing) are tested. The OpenAI call
is a single thin wrapper — trust the SDK, don't test it.
"""

from __future__ import annotations

import os
from datetime import date as _date
from pathlib import Path
from typing import Optional

from openai import OpenAI

from utils.config import get
from utils.logger import get_logger

log = get_logger(__name__)


_DAILY_SYSTEM = """You are WheelBot's daily-reflection analyst. After each trading day,
you write a short retrospective (Markdown, under 400 words) that the *next day's*
morning scan will read. Be specific and decisive.

Conventions the next scan looks for:
- If a stock should be dropped from the wishlist, use the exact phrase:
  REMOVE: <TICKER> - <reason>   (one line, uppercase REMOVE:).
- If a stock behaved especially well, use: FAVOR: <TICKER> - <reason>.
- If a parameter (delta, DTE, profit target) needs tweaking, note it plainly.

Do not pad. If nothing interesting happened, say "Quiet day, no changes."
"""


_WEEKLY_SYSTEM = """You are WheelBot's weekly strategist. Once a week you write a
concise autopsy (Markdown, under 700 words) reviewing the week and proposing
parameter changes for next week.

Structure:
1. **Numbers** — win rate, total P&L, best/worst stock, assignment count.
2. **What worked** — 2-3 bullets.
3. **What didn't** — 2-3 bullets, honest.
4. **Parameter changes** — each as: CHANGE <param> <old> -> <new> - <rationale>.
   Only propose changes if you have ≥5 closed trades as evidence. If not,
   say "Insufficient sample — hold parameters."

No filler. Be the analyst you'd want if this were your money."""


def build_daily_prompt(
    *,
    date: _date,
    fills: list[dict],
    open_positions: list[dict],
    account: dict,
) -> str:
    """Assemble the user-turn prompt for the daily reflection."""
    parts = [
        f"Date: {date.isoformat()}",
        "",
        "## Account",
        f"Portfolio value: ${account.get('portfolio_value', 0):,.2f}",
        f"Buying power: ${account.get('buying_power', 0):,.2f}",
        "",
        "## Today's fills",
    ]
    if not fills:
        parts.append("(no trades placed today — 0 fills)")
    else:
        for f in fills:
            parts.append(
                f"- {f.get('side', '?').upper()} {f.get('symbol', '?')} "
                f"{f.get('contracts', '?')}x @ ${f.get('price', 0):.2f} "
                f"({f.get('strategy', '?')})"
            )
    parts += [
        "",
        "## Open positions",
    ]
    if not open_positions:
        parts.append("(none)")
    else:
        for p in open_positions:
            parts.append(f"- {p.get('symbol')} {p.get('strategy')} @ {p.get('strike')} exp {p.get('expiration')}")
    parts += [
        "",
        "## Your task",
        "Write a reflection using the conventions in the system prompt.",
        "If any stock in today's fills should be REMOVED from the wishlist, flag it.",
    ]
    return "\n".join(parts)


def build_weekly_prompt(
    *,
    week_ending: _date,
    fills: list[dict],
    closed_trades: list[dict],
    account: dict,
) -> str:
    """Assemble the user-turn prompt for the weekly autopsy."""
    parts = [
        f"Week ending: {week_ending.isoformat()}",
        "",
        "## Account snapshot",
        f"Portfolio value: ${account.get('portfolio_value', 0):,.2f}",
        f"Buying power: ${account.get('buying_power', 0):,.2f}",
        "",
        f"## This week ({len(fills)} fills, {len(closed_trades)} closed round-trips)",
    ]
    if fills:
        for f in fills:
            parts.append(
                f"- {f.get('fill_date', '')[:10]} {f.get('side', '?').upper()} "
                f"{f.get('symbol', '?')} @ ${f.get('price', 0):.2f}"
            )
    else:
        parts.append("(no fills this week)")

    parts += ["", "## Closed trades"]
    if closed_trades:
        for t in closed_trades:
            parts.append(
                f"- {t.get('symbol')} {t.get('strategy')}: "
                f"pnl=${t.get('pnl_dollars', 0):.2f} ({t.get('pnl_percent', 0):.1f}%)"
            )
    else:
        parts.append("(none — no round-trips closed this week)")

    parts += [
        "",
        "## Your task",
        "Write a weekly autopsy. Propose specific parameter tuning changes only if ≥5 closed trades support them.",
    ]
    return "\n".join(parts)


def write_reflection(directory: Path, date: _date, content: str) -> Path:
    """Write the reflection markdown to `{directory}/{YYYY-MM-DD}.md`.

    Overwrites if it already exists (rerunning same-day replaces, not appends).
    Creates the directory if missing.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{date.isoformat()}.md"
    path.write_text(content)
    return path


class ReflectionGenerator:
    """Thin OpenAI wrapper that calls the chat API with the prompts above."""

    def __init__(self, model: Optional[str] = None):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            log.warning("OPENAI_API_KEY not set — reflection generation disabled")
            self.client = None
            return
        self.client = OpenAI(api_key=api_key)
        self.model = model or get("ai.model", "gpt-4o")

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def generate_daily(self, prompt: str) -> Optional[str]:
        if not self.enabled:
            return None
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _DAILY_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            return resp.choices[0].message.content
        except Exception as exc:
            log.error("Daily reflection generation failed: %s", exc)
            return None

    def generate_weekly(self, prompt: str) -> Optional[str]:
        if not self.enabled:
            return None
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _WEEKLY_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            return resp.choices[0].message.content
        except Exception as exc:
            log.error("Weekly autopsy generation failed: %s", exc)
            return None
