"""Main WheelBot Discord bot with APScheduler jobs and task loops."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord.ext import commands, tasks

from data.models import SignalAction, SignalStatus
from discord_bot.embeds import (
    alert_embed,
    exit_embed,
    fill_embed,
    performance_embed,
    portfolio_embed,
    roll_embed,
    signal_embed,
)
from discord_bot.views import LEAPSApprovalView, RollApprovalView, TradeApprovalView
from discord_bot.webhook import WebhookSender
from utils.config import get as cfg_get
from utils.logger import get_logger
from utils.timing import is_market_open, is_trading_day, now_et

if TYPE_CHECKING:
    from engine.signal import SignalQueue

log = get_logger(__name__)


# ── Bot class ─────────────────────────────────────────────────────────────


class WheelBot(commands.Bot):
    """Discord bot for WheelBot options trading system."""

    def __init__(
        self,
        broker: object,
        signal_queue: SignalQueue,
        executor: object,
        scanner: object,
        exit_engine: object,
        order_tracker: object,
        reconciler: object,
        performance_tracker: object,
        webhook_sender: WebhookSender | None = None,
        heartbeat: object | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        # Core component references
        self.broker = broker
        self.signal_queue = signal_queue
        self.executor = executor
        self.scanner = scanner
        self.exit_engine = exit_engine
        self.order_tracker = order_tracker
        self.reconciler = reconciler
        self.performance_tracker = performance_tracker
        self.webhook_sender = webhook_sender
        self.heartbeat = heartbeat

        # Scheduler (created in setup_hook)
        self.scheduler: AsyncIOScheduler | None = None

        # Channel for trade alerts
        self._channel_id = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
        self._channel: discord.TextChannel | None = None

        # Silent-failure alarm state
        from utils.timing import now_et as _now_et
        self._bot_started_at = _now_et()
        self._silent_alarm_last_fired_date = None  # type: ignore[assignment]

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        """Called when the bot is starting — register commands, schedule jobs."""
        log.info("WheelBot setup_hook: registering commands and starting tasks")

        # Register slash commands
        self.tree.add_command(_portfolio_cmd)
        self.tree.add_command(_performance_cmd)
        self.tree.add_command(_scan_cmd)
        self.tree.add_command(_signals_cmd)

        # Start background task loops
        self._order_tracker_loop.start()
        self._heartbeat_loop.start()
        self._exit_monitor_loop.start()
        self._fast_exit_monitor_loop.start()

        # APScheduler for time-specific jobs (all times in US/Eastern)
        self.scheduler = AsyncIOScheduler(timezone="America/New_York")

        self.scheduler.add_job(
            self._job_premarket_check,
            CronTrigger(hour=8, minute=0),
            id="premarket_check",
            name="Pre-market check",
        )
        self.scheduler.add_job(
            self._job_assignment_reconciliation,
            CronTrigger(hour=9, minute=31),
            id="assignment_reconciliation",
            name="Assignment reconciliation",
        )
        self.scheduler.add_job(
            self._job_morning_scan,
            CronTrigger(hour=9, minute=35),
            id="morning_scan",
            name="Morning scan",
        )
        self.scheduler.add_job(
            self._job_auto_cancel_unfilled,
            CronTrigger(hour=15, minute=50),
            id="auto_cancel_unfilled",
            name="Auto-cancel unfilled orders",
        )
        self.scheduler.add_job(
            self._job_daily_snapshot,
            CronTrigger(hour=17, minute=0),
            id="daily_snapshot",
            name="Daily snapshot + performance",
        )
        self.scheduler.add_job(
            self._job_daily_reflection,
            CronTrigger(hour=17, minute=30, day_of_week="mon-fri"),
            id="daily_reflection",
            name="Daily reflection (AI)",
        )
        self.scheduler.add_job(
            self._job_weekly_autopsy,
            CronTrigger(day_of_week="sun", hour=10, minute=0),
            id="weekly_autopsy",
            name="Weekly autopsy (AI)",
        )

        self.scheduler.start()
        log.info("APScheduler started with %d jobs", len(self.scheduler.get_jobs()))

    async def on_ready(self) -> None:
        """Fires when the bot has connected to Discord."""
        log.info("WheelBot connected as %s (ID: %s)", self.user, self.user.id if self.user else "?")

        if self._channel_id:
            self._channel = self.get_channel(self._channel_id)  # type: ignore[assignment]
            if self._channel is None:
                try:
                    self._channel = await self.fetch_channel(self._channel_id)  # type: ignore[assignment]
                except discord.HTTPException:
                    log.error("Could not fetch channel %d", self._channel_id)

        # Sync slash commands to EACH guild (instant) instead of global (up to 1hr delay)
        try:
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild '%s'", len(synced), guild.name)
        except discord.HTTPException as exc:
            log.error("Failed to sync slash commands: %s", exc)

        # NEW-I4: Recover pending signals from previous session
        pending_signals = self.signal_queue.get_pending()
        if pending_signals:
            log.warning("Found %d pending signals from previous session — auto-executing", len(pending_signals))
            for sig in pending_signals:
                try:
                    execution = await discord.utils.maybe_coroutine(
                        self.executor.execute_signal, sig,
                    )
                    self.signal_queue.mark_auto_executed(sig.id)
                    log.info("Recovered and executed pending signal #%d: %s %s", sig.id, sig.action, sig.symbol)
                except Exception as exc:
                    log.error("Failed to recover signal #%d: %s", sig.id, exc)
                    # Expire it so it doesn't try again on next restart
                    self.signal_queue.expire_stale()

    async def close(self) -> None:
        """Graceful shutdown."""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            log.info("APScheduler shut down")
        await super().close()

    # ── Public helpers for sending messages ────────────────────────────

    async def send_signal(self, signal) -> discord.Message | None:
        """Send a signal embed with the appropriate approval buttons to the channel."""
        if not self._channel:
            log.warning("No channel configured — cannot send signal #%s", signal.id)
            return None

        # Choose embed + view based on action
        if signal.action == SignalAction.ROLL.value:
            # For rolls we need the position; fetch via broker or DB
            from data import database as db

            positions = db.get_open_positions(symbol=signal.symbol)
            position = positions[0] if positions else None
            embed = roll_embed(signal, position) if position else signal_embed(signal)
            view = RollApprovalView(signal, self.signal_queue, self.executor)
        elif signal.action == SignalAction.BUY_LEAPS.value:
            embed = signal_embed(signal)
            view = LEAPSApprovalView(signal, self.signal_queue, self.executor)
        else:
            embed = signal_embed(signal)
            view = TradeApprovalView(signal, self.signal_queue, self.executor)

        try:
            msg = await self._channel.send(embed=embed, view=view)
            view.message = msg  # Store for timeout editing
            log.info("Signal #%s sent to channel (msg %s)", signal.id, msg.id)
            return msg
        except discord.HTTPException as exc:
            log.error("Failed to send signal #%s: %s", signal.id, exc)
            return None

    async def send_exit_alert(self, signal, position) -> None:
        """Send an exit alert embed to the channel."""
        if not self._channel:
            return
        embed = exit_embed(signal, position)
        try:
            await self._channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.error("Failed to send exit alert: %s", exc)

    async def send_fill_notification(self, execution, position) -> None:
        """Send a fill notification embed to the channel."""
        if not self._channel:
            return
        embed = fill_embed(execution, position)
        try:
            await self._channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.error("Failed to send fill notification: %s", exc)

    async def send_alert(self, title: str, message: str, level: str = "warning") -> None:
        """Send a generic alert embed to the channel."""
        if not self._channel:
            return
        embed = alert_embed(title, message, level)
        try:
            await self._channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.error("Failed to send alert: %s", exc)

    # ── Background task loops (discord.ext.tasks) ─────────────────────

    @tasks.loop(minutes=5)
    async def _order_tracker_loop(self) -> None:
        """Check pending orders for fills every 5 minutes."""
        if not is_trading_day() or not is_market_open():
            return
        try:
            if hasattr(self.order_tracker, "check_pending_orders"):
                await discord.utils.maybe_coroutine(self.order_tracker.check_pending_orders)
                log.debug("Order tracker check complete")
        except Exception as exc:
            log.error("Order tracker loop error: %s", exc)

    @_order_tracker_loop.before_loop
    async def _before_order_tracker(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=10)
    async def _heartbeat_loop(self) -> None:
        """Send heartbeat ping every 10 minutes + silent-failure alarm check."""
        try:
            if self.heartbeat and hasattr(self.heartbeat, "ping"):
                await discord.utils.maybe_coroutine(self.heartbeat.ping)
                log.debug("Heartbeat ping sent")
        except Exception as exc:
            log.error("Heartbeat loop error: %s", exc)
            if self.webhook_sender:
                self.webhook_sender.send_heartbeat_alert(f"Heartbeat failed: {exc}")

        # Silent-failure check: if N trading days have passed during market hours
        # with zero fills, page the user. Dedupe: fire once per day max.
        try:
            from data import database as _db
            from engine.silent_failure_alarm import should_alarm
            from utils.timing import now_et

            _now = now_et()
            last_fill = _db.get_last_fill_date()
            if should_alarm(
                now=_now,
                last_fill_at=last_fill,
                bot_started_at=self._bot_started_at,
                trading_days_threshold=2,
            ):
                today = _now.date()
                if self._silent_alarm_last_fired_date != today:
                    self._silent_alarm_last_fired_date = today
                    ref = last_fill or self._bot_started_at
                    msg = (
                        f"🚨 SILENT-FAILURE ALARM\n"
                        f"No fills detected in 2+ trading days.\n"
                        f"Last fill: {ref.isoformat() if ref else 'never'}\n"
                        f"Now: {_now.isoformat()}\n"
                        f"Check Railway logs + Alpaca auth."
                    )
                    log.warning(msg)
                    if self.webhook_sender:
                        self.webhook_sender.send_heartbeat_alert(msg)
        except Exception as exc:
            log.error("Silent-failure alarm check failed: %s", exc)

    @_heartbeat_loop.before_loop
    async def _before_heartbeat(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=5)
    async def _exit_monitor_loop(self) -> None:
        """Check positions for exit conditions every 5 minutes (was 15)."""
        if not is_trading_day() or not is_market_open():
            return
        try:
            if hasattr(self.exit_engine, "check_all_positions"):
                signals = await discord.utils.maybe_coroutine(
                    self.exit_engine.check_all_positions,
                )
            elif hasattr(self.exit_engine, "check_positions"):
                signals = await discord.utils.maybe_coroutine(
                    self.exit_engine.check_positions,
                )
            else:
                signals = None

            if signals:
                for sig in signals:
                    if sig.status == SignalStatus.AUTO_EXECUTED.value:
                        # Already executed — send notification only, no buttons
                        embed = signal_embed(sig)
                        embed.set_footer(text="AUTO-EXECUTED")
                        embed.color = discord.Colour.green()
                        if self._channel:
                            await self._channel.send(embed=embed)
                    else:
                        await self.send_signal(sig)
                log.info("Exit monitor found %d exit signals", len(signals))
        except Exception as exc:
            log.error("Exit monitor loop error: %s", exc)

    @_exit_monitor_loop.before_loop
    async def _before_exit_monitor(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def _fast_exit_monitor_loop(self) -> None:
        """Fast loop (every 1 min) for positions near their stop-loss level."""
        if not is_trading_day() or not is_market_open():
            return
        try:
            if not hasattr(self.exit_engine, "has_near_stop_positions"):
                return
            has_near = await discord.utils.maybe_coroutine(
                self.exit_engine.has_near_stop_positions,
            )
            if not has_near:
                return

            log.info("Near-stop positions detected — running fast exit check")
            if hasattr(self.exit_engine, "check_all_positions"):
                signals = await discord.utils.maybe_coroutine(
                    self.exit_engine.check_all_positions,
                )
            elif hasattr(self.exit_engine, "check_positions"):
                signals = await discord.utils.maybe_coroutine(
                    self.exit_engine.check_positions,
                )
            else:
                return

            if signals:
                for sig in signals:
                    if sig.status == SignalStatus.AUTO_EXECUTED.value:
                        # Already executed — send notification only, no buttons
                        embed = signal_embed(sig)
                        embed.set_footer(text="AUTO-EXECUTED")
                        embed.color = discord.Colour.green()
                        if self._channel:
                            await self._channel.send(embed=embed)
                    else:
                        await self.send_signal(sig)
                log.info("Fast exit monitor: %d exit signals", len(signals))
        except Exception as exc:
            log.error("Fast exit monitor loop error: %s", exc)

    @_fast_exit_monitor_loop.before_loop
    async def _before_fast_exit_monitor(self) -> None:
        await self.wait_until_ready()

    # ── APScheduler jobs ──────────────────────────────────────────────

    async def _job_premarket_check(self) -> None:
        """08:00 ET — Check positions for overnight moves > 5%."""
        if not is_trading_day():
            return
        log.info("Running pre-market check")
        try:
            if hasattr(self.broker, "get_positions"):
                positions = await discord.utils.maybe_coroutine(
                    self.broker.get_positions,
                )
                large_moves = []
                for pos in positions:
                    if (
                        pos.pnl_percent is not None
                        and abs(pos.pnl_percent) > 5.0
                    ):
                        large_moves.append(pos)

                if large_moves:
                    symbols = ", ".join(p.symbol for p in large_moves)
                    await self.send_alert(
                        "Overnight Moves > 5%",
                        f"Large moves detected: {symbols}",
                        level="warning",
                    )
                    log.info("Pre-market: %d large overnight moves", len(large_moves))
        except Exception as exc:
            log.error("Pre-market check failed: %s", exc)

    async def _job_assignment_reconciliation(self) -> None:
        """09:31 ET — Reconcile assignments from overnight."""
        if not is_trading_day():
            return
        log.info("Running assignment reconciliation")
        try:
            if hasattr(self.reconciler, "reconcile"):
                result = await discord.utils.maybe_coroutine(self.reconciler.reconcile)
                if result:
                    await self.send_alert(
                        "Assignment Reconciliation",
                        f"Reconciliation complete: {result}",
                        level="info",
                    )
        except Exception as exc:
            log.error("Assignment reconciliation failed: %s", exc)

    async def _job_morning_scan(self) -> None:
        """09:35 ET — Run Wheel strategy scan."""
        if not is_trading_day():
            return
        log.info("Running morning scan")
        all_signals = []
        try:
            # PRIMARY: Wheel strategy scan
            if hasattr(self, "wheel_strategy") and self.wheel_strategy:
                wishlist = cfg_get("wheel.wishlist", [])
                wheel_signals = await discord.utils.maybe_coroutine(
                    self.wheel_strategy.scan_for_entries, wishlist,
                )
                if wheel_signals:
                    all_signals.extend(wheel_signals)
                    log.info("Wheel scan: %d signals", len(wheel_signals))
                else:
                    log.info("Wheel scan: no qualifying trades found")

            # AUTO-EXECUTE all signals and notify via Discord (no approval needed)
            # Track cumulative capital committed during this scan to prevent
            # two CSPs from together exceeding buying power (Alpaca doesn't
            # instantly reflect margin reservation after order placement).
            if all_signals:
                usable_capital = await discord.utils.maybe_coroutine(
                    self.broker.get_buying_power,
                )
                capital_committed = 0.0

                for sig in all_signals:
                    # Check if cumulative capital would exceed safe limit
                    if sig.action == 'sell_csp':  # Only CSPs need collateral; CCs are covered by held shares
                        capital_committed += (sig.strike or 0) * 100
                        if capital_committed > usable_capital * 0.90:  # Leave 10% buffer
                            log.warning(
                                "Skipping %s — cumulative capital $%.0f would exceed "
                                "safe limit ($%.0f * 90%%)",
                                sig.symbol, capital_committed, usable_capital,
                            )
                            capital_committed -= (sig.strike or 0) * 100  # Undo the add
                            continue

                    sig_id = self.signal_queue.create(sig)
                    sig.id = sig_id

                    # Auto-execute immediately
                    try:
                        execution = await discord.utils.maybe_coroutine(
                            self.executor.execute_signal, sig,
                        )
                        self.signal_queue.mark_auto_executed(sig_id)

                        # Notify on Discord (no buttons — just a confirmation)
                        embed = signal_embed(sig)
                        embed.set_footer(text="✅ AUTO-EXECUTED | WheelBot Autonomous Mode")
                        embed.color = discord.Colour.green()
                        if self._channel:
                            await self._channel.send(embed=embed)

                        log.info("Auto-executed signal #%d: %s", sig_id, sig.reason[:80])
                    except Exception as exec_err:
                        log.error("Auto-execution failed for signal #%d: %s", sig_id, exec_err)
                        await self.send_alert(
                            "Execution Failed",
                            f"Signal #{sig_id} failed: {exec_err}",
                            level="error",
                        )

                log.info("Morning scan: %d signals auto-executed", len(all_signals))
            else:
                log.info("Morning scan: no signals today")
        except Exception as exc:
            log.error("Morning scan failed: %s", exc)
            await self.send_alert("Morning Scan Error", str(exc), level="error")

    async def _job_auto_cancel_unfilled(self) -> None:
        """15:50 ET — Cancel orders that haven't filled."""
        if not is_trading_day():
            return
        log.info("Auto-cancelling unfilled orders")
        try:
            if hasattr(self.order_tracker, "cancel_all_pending"):
                cancelled = await discord.utils.maybe_coroutine(
                    self.order_tracker.cancel_all_pending,
                )
                if cancelled:
                    await self.send_alert(
                        "Orders Auto-Cancelled",
                        f"Cancelled {cancelled} unfilled order(s) before close.",
                        level="info",
                    )
        except Exception as exc:
            log.error("Auto-cancel unfilled failed: %s", exc)

    async def _job_daily_snapshot(self) -> None:
        """17:00 ET — Daily portfolio snapshot + performance update."""
        if not is_trading_day():
            return
        log.info("Running daily snapshot")
        try:
            # Performance update
            if hasattr(self.performance_tracker, "compute_daily"):
                perf = await discord.utils.maybe_coroutine(
                    self.performance_tracker.compute_daily,
                )
                if perf and self._channel:
                    perf_dict = perf if isinstance(perf, dict) else perf.__dict__
                    embed = performance_embed(perf_dict)
                    await self._channel.send(embed=embed)

            # Portfolio snapshot
            from data import database as db

            positions = db.get_open_positions()
            if self._channel:
                embed = portfolio_embed(positions)
                await self._channel.send(embed=embed)

            log.info("Daily snapshot complete")
        except Exception as exc:
            log.error("Daily snapshot failed: %s", exc)

    async def _job_daily_reflection(self) -> None:
        """17:30 ET Mon-Fri — AI reflection written to reflections/YYYY-MM-DD.md."""
        if not is_trading_day():
            return
        log.info("Running daily reflection")
        try:
            from pathlib import Path

            from ai.reflections import ReflectionGenerator, build_daily_prompt, write_reflection
            from data import database as _db
            from utils.timing import now_et

            gen = ReflectionGenerator()
            if not gen.enabled:
                log.warning("Reflection skipped — OpenAI key not configured")
                return

            today = now_et().date()
            # Gather today's fills from DB
            fills = self._collect_todays_fills(today)
            open_positions = [
                {"symbol": p.symbol, "strategy": p.strategy, "strike": p.strike,
                 "expiration": p.expiration_date}
                for p in _db.get_open_positions()
            ]
            account = {}
            try:
                acct = self.broker.get_account_info()
                account = {"portfolio_value": acct.portfolio_value, "buying_power": acct.buying_power}
            except Exception:
                pass

            prompt = build_daily_prompt(
                date=today, fills=fills, open_positions=open_positions, account=account,
            )
            content = gen.generate_daily(prompt)
            if not content:
                log.warning("Daily reflection: OpenAI returned empty")
                return

            reflections_dir = Path(__file__).parent.parent / "reflections"
            path = write_reflection(reflections_dir, today, content)
            log.info("Daily reflection written: %s", path)

            if self._channel:
                snippet = content[:900] + ("..." if len(content) > 900 else "")
                await self._channel.send(f"📓 **Daily reflection — {today}**\n```\n{snippet}\n```")
        except Exception as exc:
            log.error("Daily reflection failed: %s", exc)

    async def _job_weekly_autopsy(self) -> None:
        """Sunday 10:00 ET — Weekly autopsy written to reflections/weekly-YYYY-MM-DD.md."""
        log.info("Running weekly autopsy")
        try:
            from pathlib import Path

            from ai.reflections import ReflectionGenerator, build_weekly_prompt, write_reflection
            from data import database as _db
            from utils.timing import now_et

            gen = ReflectionGenerator()
            if not gen.enabled:
                log.warning("Weekly autopsy skipped — OpenAI key not configured")
                return

            today = now_et().date()
            # Previous 7 days of fills + any closed trades in that window
            fills = self._collect_weekly_fills(today)
            closed_trades = [
                {"symbol": p.symbol, "strategy": p.strategy,
                 "pnl_dollars": p.pnl_dollars or 0, "pnl_percent": p.pnl_percent or 0}
                for p in _db.get_closed_trades()
            ][-20:]  # last 20 is plenty
            account = {}
            try:
                acct = self.broker.get_account_info()
                account = {"portfolio_value": acct.portfolio_value, "buying_power": acct.buying_power}
            except Exception:
                pass

            prompt = build_weekly_prompt(
                week_ending=today, fills=fills, closed_trades=closed_trades, account=account,
            )
            content = gen.generate_weekly(prompt)
            if not content:
                log.warning("Weekly autopsy: OpenAI returned empty")
                return

            reflections_dir = Path(__file__).parent.parent / "reflections"
            # Prefix "weekly-" so daily files and weekly files don't clash
            # Use a custom write since the helper uses plain YYYY-MM-DD.md naming
            reflections_dir.mkdir(parents=True, exist_ok=True)
            path = reflections_dir / f"weekly-{today.isoformat()}.md"
            path.write_text(content)
            log.info("Weekly autopsy written: %s", path)

            if self._channel:
                snippet = content[:1400] + ("..." if len(content) > 1400 else "")
                await self._channel.send(f"📊 **Weekly autopsy — week ending {today}**\n```\n{snippet}\n```")
        except Exception as exc:
            log.error("Weekly autopsy failed: %s", exc)

    def _collect_todays_fills(self, today) -> list[dict]:
        """Pull today's filled executions from the DB as simple dicts."""
        from data.database import _connect
        with _connect() as conn:
            rows = conn.execute(
                "SELECT e.*, s.symbol, s.strategy, s.side FROM executions e "
                "LEFT JOIN signals s ON e.signal_id = s.id "
                "WHERE e.fill_date IS NOT NULL "
                "AND DATE(e.fill_date) = DATE(?)",
                (today.isoformat(),),
            ).fetchall()
        return [
            {"symbol": r["symbol"] or "?", "strategy": r["strategy"] or "?",
             "side": r["side"] or "sell", "price": r["fill_price"] or 0,
             "contracts": 1, "fill_date": str(r["fill_date"])}
            for r in rows
        ]

    def _collect_weekly_fills(self, today) -> list[dict]:
        """Pull last 7 days of fills from the DB."""
        from datetime import timedelta

        from data.database import _connect
        start = (today - timedelta(days=7)).isoformat()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT e.*, s.symbol, s.strategy, s.side FROM executions e "
                "LEFT JOIN signals s ON e.signal_id = s.id "
                "WHERE e.fill_date IS NOT NULL AND DATE(e.fill_date) >= DATE(?) "
                "ORDER BY e.fill_date",
                (start,),
            ).fetchall()
        return [
            {"symbol": r["symbol"] or "?", "strategy": r["strategy"] or "?",
             "side": r["side"] or "sell", "price": r["fill_price"] or 0,
             "contracts": 1, "fill_date": str(r["fill_date"])}
            for r in rows
        ]


# ── Slash commands (module-level, added in setup_hook) ────────────────────


@discord.app_commands.command(name="portfolio", description="Show all open positions")
async def _portfolio_cmd(interaction: discord.Interaction) -> None:
    from data import database as db

    positions = db.get_open_positions()
    embed = portfolio_embed(positions)
    await interaction.response.send_message(embed=embed)


@discord.app_commands.command(name="performance", description="Show performance stats")
async def _performance_cmd(interaction: discord.Interaction) -> None:
    from data import database as db

    perf = db.get_performance()
    if perf:
        perf_dict = perf.__dict__ if hasattr(perf, "__dict__") else perf
        embed = performance_embed(perf_dict)
    else:
        embed = alert_embed("Performance", "No performance data available yet.", level="info")
    await interaction.response.send_message(embed=embed)


@discord.app_commands.command(name="scan", description="Manually trigger a market scan")
async def _scan_cmd(interaction: discord.Interaction) -> None:
    bot: WheelBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True)

    try:
        all_signals = []

        # Wheel strategy scan
        if hasattr(bot, "wheel_strategy") and bot.wheel_strategy:
            from utils.config import get as cfg_get_local
            wishlist = cfg_get_local("wheel.wishlist", [])
            wheel_signals = await discord.utils.maybe_coroutine(
                bot.wheel_strategy.scan_for_entries, wishlist,
            )
            if wheel_signals:
                all_signals.extend(wheel_signals)

        if all_signals:
            # NEW-I1: Get buying power and track cumulative capital
            try:
                usable_capital = await discord.utils.maybe_coroutine(bot.broker.get_buying_power) if bot.broker else 100000
            except Exception:
                usable_capital = 100000

            capital_committed = 0.0
            executed_count = 0

            for sig in all_signals:
                # NEW-I1: Check if cumulative capital would exceed safe limit
                if sig.action == 'sell_csp':  # Only CSPs need collateral; CCs are covered by held shares
                    capital_committed += (sig.strike or 0) * 100
                    if capital_committed > usable_capital * 0.90:
                        log.warning("Skipping %s in /scan — capital limit reached", sig.symbol)
                        capital_committed -= (sig.strike or 0) * 100  # Undo the add
                        continue

                sig_id = bot.signal_queue.create(sig)
                sig.id = sig_id

                # Auto-execute
                try:
                    execution = await discord.utils.maybe_coroutine(
                        bot.executor.execute_signal, sig,
                    )
                    bot.signal_queue.mark_auto_executed(sig_id)
                    executed_count += 1
                except Exception as exec_err:
                    log.error("Execution failed for signal #%d: %s", sig_id, exec_err)

                # Send notification embed
                embed = signal_embed(sig)
                embed.set_footer(text="✅ AUTO-EXECUTED | WheelBot Autonomous Mode")
                embed.color = discord.Colour.green()
                await interaction.followup.send(embed=embed)

            await interaction.followup.send(
                f"✅ Scan complete — {executed_count} trade(s) auto-executed.",
            )
        else:
            await interaction.followup.send(
                "Scan complete — no qualifying trades found right now. "
                "VIX may be too low or position limits reached."
            )
    except Exception as exc:
        log.error("Manual scan failed: %s", exc)
        await interaction.followup.send(f"Scan error: {exc}")


@discord.app_commands.command(name="signals", description="Show pending signals")
async def _signals_cmd(interaction: discord.Interaction) -> None:
    bot: WheelBot = interaction.client  # type: ignore[assignment]
    pending = bot.signal_queue.get_pending()

    if not pending:
        await interaction.response.send_message("No pending signals.")
        return

    lines = []
    for sig in pending:
        strike_str = f"${sig.strike:.2f}" if sig.strike else ""
        lines.append(
            f"**#{sig.id}** {sig.action} {sig.symbol} {strike_str} "
            f"{sig.expiration_date or ''} — {sig.urgency}",
        )

    embed = discord.Embed(
        title=f"Pending Signals ({len(pending)})",
        description="\n".join(lines),
        color=discord.Colour.orange(),
    )
    await interaction.response.send_message(embed=embed)


# ── Factory function ──────────────────────────────────────────────────────


def create_bot(
    broker: object | None = None,
    signal_queue: SignalQueue | None = None,
    executor: object | None = None,
    scanner: object | None = None,
    exit_engine: object | None = None,
    order_tracker: object | None = None,
    reconciler: object | None = None,
    performance_tracker: object | None = None,
    webhook_sender: WebhookSender | None = None,
    heartbeat: object | None = None,
    ai_researcher: object | None = None,
    universe: object | None = None,
    sizer: object | None = None,
    wheel_strategy: object | None = None,
) -> WheelBot:
    """Factory: build and return a fully configured WheelBot instance."""
    bot = WheelBot(
        broker=broker,
        signal_queue=signal_queue,
        executor=executor,
        scanner=scanner,
        exit_engine=exit_engine,
        order_tracker=order_tracker,
        reconciler=reconciler,
        performance_tracker=performance_tracker,
        webhook_sender=webhook_sender,
        heartbeat=heartbeat,
    )
    # Attach extra components
    bot.ai_researcher = ai_researcher
    bot.universe = universe
    bot.sizer = sizer
    bot.wheel_strategy = wheel_strategy
    log.info("WheelBot instance created")
    return bot
