"""WheelBot — AI-Powered Options Trading Bot.

Entry point: initializes all components, starts Discord bot with scheduled jobs.

Scheduled jobs (all times ET):
  08:00 — Pre-market position check (overnight moves)
  09:31 — Assignment reconciliation
  09:35 — Morning scan + AI research → signals
  Every 5 min — Order tracker (pending fills)
  Every 10 min — Heartbeat ping (API health)
  Every 15 min — Exit engine (position monitor)
  15:50 — Auto-cancel unfilled orders
  17:00 — Daily snapshot + performance update
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

from ai.researcher import AIResearcher
from broker.alpaca_broker import AlpacaBroker
from data import database as db
from data.universe import StockUniverse
from discord_bot.webhook import WebhookSender
from engine.executor import OrderExecutor
from engine.exit_engine import ExitEngine
from engine.order_tracker import OrderTracker
from engine.performance import PerformanceTracker
from engine.position_sizer import PositionSizer
from engine.reconciler import PositionReconciler
from engine.scanner import StockScanner
from engine.signal import SignalQueue
from strategies.wheel import WheelStrategy
from utils.config import get, load_config
from utils.heartbeat import HeartbeatMonitor
from utils.logger import get_logger

log = get_logger("wheelbot")


def main():
    """Initialize all components and start the bot."""
    log.info("=" * 60)
    log.info("WheelBot starting up...")
    log.info("=" * 60)

    # Load config
    config = load_config()
    paper_trade = get("broker.paper_trade", True)
    if paper_trade:
        log.info("*** PAPER TRADE MODE — no real orders will be placed ***")
    else:
        log.info("*** LIVE TRADING MODE ***")

    # Log resolved config values so they're verifiable from logs (no more guessing
    # which config was deployed — was the #1 silent-failure class historically)
    log.info(
        "Config loaded: capital.total=$%s | max_per_position=%s%% | reserve=%s%% | "
        "margin=%s | max_open=%s | wheel.dte=[%s,%s] | wheel.profit_target=%s%% | "
        "wheel.target_delta=%s | auto_execute=%s | wishlist=%s",
        get("capital.total", 0),
        int(get("capital.max_per_position_pct", 0) * 100),
        int(get("capital.reserve_pct", 0) * 100),
        get("capital.margin_enabled", False),
        get("positions.max_open_total", 0),
        get("wheel.target_dte_min", 30),
        get("wheel.target_dte_max", 45),
        int(get("wheel.profit_target_pct", 0.5) * 100),
        get("wheel.target_delta", 0.20),
        get("broker.auto_execute", False),
        ",".join(get("wheel.wishlist", []) or []),
    )

    # Initialize database — clean slate on fresh deploy (Railway containers are ephemeral)
    import pathlib
    db_path = pathlib.Path(__file__).parent / "wheelbot.db"
    if db_path.exists():
        # Check if DB has ghost positions from old fake paper mode
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM positions WHERE notes LIKE '%Order pending%' OR notes IS NULL").fetchone()[0]
            if count > 0:
                log.warning("Clearing %d ghost positions from old paper mode", count)
                conn.execute("DELETE FROM positions")
                conn.execute("DELETE FROM signals")
                conn.execute("DELETE FROM executions")
                conn.commit()
        except Exception:
            pass  # Table might not exist yet
        finally:
            conn.close()

    db.init_db()
    log.info("Database initialized")

    # Initialize Alpaca broker
    try:
        broker = AlpacaBroker(paper=paper_trade)
        acct = broker.get_account_info()
        log.info(
            "Alpaca connected (%s) — buying power: $%.2f, portfolio: $%.2f",
            "paper" if paper_trade else "LIVE",
            acct.buying_power,
            acct.portfolio_value,
        )
    except Exception as e:
        log.error("Failed to connect to Alpaca: %s", e)
        sys.exit(1)

    # Initialize webhook for alerts that don't need the bot
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    webhook = WebhookSender(webhook_url)

    # Initialize components
    signal_queue = SignalQueue()
    executor = OrderExecutor(broker)
    sizer = PositionSizer()
    universe = StockUniverse()
    scanner = StockScanner(broker, universe, sizer)
    wheel = WheelStrategy(broker, db)
    exit_engine = ExitEngine(broker, signal_queue, executor)
    order_tracker = OrderTracker(broker, webhook)
    reconciler = PositionReconciler(broker, db)
    performance = PerformanceTracker(broker)
    heartbeat = HeartbeatMonitor(broker, webhook_url)
    ai = AIResearcher()

    log.info("All components initialized")

    # Start Discord bot
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not bot_token:
        log.error("DISCORD_BOT_TOKEN not set. Exiting.")
        sys.exit(1)

    from discord_bot.bot import create_bot

    bot = create_bot(
        broker=broker,
        signal_queue=signal_queue,
        executor=executor,
        scanner=scanner,
        exit_engine=exit_engine,
        order_tracker=order_tracker,
        reconciler=reconciler,
        performance_tracker=performance,
        webhook_sender=webhook,
        heartbeat=heartbeat,
        ai_researcher=ai,
        universe=universe,
        sizer=sizer,
        wheel_strategy=wheel,
    )

    log.info("Starting Discord bot...")
    bot.run(bot_token, log_handler=None)


if __name__ == "__main__":
    main()
