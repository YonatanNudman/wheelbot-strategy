"""WheelBot Trading Dashboard — mobile-first FastAPI app.

Run:
    python dashboard.py
    # or
    uvicorn dashboard:app --host 0.0.0.0 --port 8888 --reload
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "wheelbot.db"
CONFIG_PATH = Path(__file__).parent / "config.yaml"

# ── Alpaca client (lazy singleton) ────────────────────────────────────────

_alpaca = None


def _get_alpaca():
    """Lazily create AlpacaBroker so the dashboard still works without keys."""
    global _alpaca
    if _alpaca is not None:
        return _alpaca
    try:
        from broker.alpaca_broker import AlpacaBroker

        cfg = _load_config()
        paper = cfg.get("broker", {}).get("paper_trade", True)
        _alpaca = AlpacaBroker(paper=paper)
        return _alpaca
    except Exception:
        return None


# ── Config helpers ────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


# ── DB helpers (standalone, no import of data.database) ───────────────────

def _db_query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read query and return list of dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    yield


app = FastAPI(title="WheelBot Dashboard", lifespan=lifespan)

# ── API Endpoints ─────────────────────────────────────────────────────────


@app.get("/api/account")
async def api_account():
    """Alpaca account info + bot status."""
    cfg = _load_config()
    paper = cfg.get("broker", {}).get("paper_trade", True)

    broker = _get_alpaca()
    if broker is None:
        return JSONResponse({
            "portfolio_value": 0,
            "buying_power": 0,
            "cash": 0,
            "day_pnl": 0,
            "mode": "paper" if paper else "live",
            "status": "offline",
            "error": "Alpaca keys not configured",
        })

    try:
        acct = broker.trading.get_account()
        # Compute day P&L from equity vs last_equity
        equity = float(acct.equity)
        last_equity = float(acct.last_equity) if acct.last_equity else equity
        day_pnl = equity - last_equity

        return JSONResponse({
            "portfolio_value": float(acct.portfolio_value),
            "buying_power": float(acct.buying_power),
            "cash": float(acct.cash),
            "day_pnl": round(day_pnl, 2),
            "mode": "paper" if paper else "live",
            "status": "running",
        })
    except Exception as exc:
        return JSONResponse({
            "portfolio_value": 0,
            "buying_power": 0,
            "cash": 0,
            "day_pnl": 0,
            "mode": "paper" if paper else "live",
            "status": "error",
            "error": str(exc),
        })


@app.get("/api/positions")
async def api_positions():
    """Open positions from DB."""
    rows = _db_query(
        "SELECT * FROM positions WHERE state = 'open' ORDER BY entry_date DESC"
    )
    return JSONResponse(rows)


@app.get("/api/signals")
async def api_signals():
    """Recent signals (last 20)."""
    rows = _db_query(
        "SELECT * FROM signals ORDER BY created_at DESC LIMIT 20"
    )
    return JSONResponse(rows)


@app.get("/api/executions")
async def api_executions():
    """Recent executions (last 20)."""
    rows = _db_query(
        "SELECT * FROM executions ORDER BY created_at DESC LIMIT 20"
    )
    return JSONResponse(rows)


@app.get("/api/performance")
async def api_performance():
    """Performance stats."""
    rows = _db_query("SELECT * FROM performance ORDER BY strategy, period")
    return JSONResponse(rows)


@app.get("/api/snapshots")
async def api_snapshots():
    """Portfolio value history for chart."""
    rows = _db_query(
        "SELECT date, total_account_value, cash_balance, positions_value, "
        "day_pnl, total_pnl, total_pnl_pct "
        "FROM portfolio_snapshots ORDER BY date ASC"
    )
    return JSONResponse(rows)


@app.post("/api/config")
async def api_config_update(body: dict):
    """Update config values."""
    cfg = _load_config()

    field_map = {
        "paper_trade": ("broker", "paper_trade"),
        "vrp_enabled": ("vrp_spreads", "enabled"),
        "pmcc_enabled": ("pmcc", "enabled"),
        "wheel_enabled": ("wheel", "enabled"),
        "max_simultaneous": ("vrp_spreads", "max_simultaneous"),
        "profit_target_pct": ("vrp_spreads", "profit_target_pct"),
        "stop_loss_multiplier": ("vrp_spreads", "stop_loss_multiplier"),
        "max_open_total": ("positions", "max_open_total"),
    }

    changes = {}
    for key, value in body.items():
        if key in field_map:
            section, field = field_map[key]
            if section not in cfg:
                cfg[section] = {}
            # Coerce types
            if isinstance(value, str):
                if value.lower() in ("true", "false"):
                    value = value.lower() == "true"
                else:
                    try:
                        value = float(value)
                        if value == int(value):
                            value = int(value)
                    except ValueError:
                        pass
            cfg[section][field] = value
            changes[key] = value

    _save_config(cfg)

    # Reset Alpaca client if paper_trade changed
    if "paper_trade" in changes:
        global _alpaca
        _alpaca = None

    return JSONResponse({"ok": True, "updated": changes})


@app.get("/api/config")
async def api_config_read():
    """Read current config values for the settings panel."""
    cfg = _load_config()
    return JSONResponse({
        "paper_trade": cfg.get("broker", {}).get("paper_trade", True),
        "vrp_enabled": cfg.get("vrp_spreads", {}).get("enabled", False),
        "pmcc_enabled": cfg.get("pmcc", {}).get("enabled", False),
        "wheel_enabled": cfg.get("wheel", {}).get("enabled", False),
        "max_simultaneous": cfg.get("vrp_spreads", {}).get("max_simultaneous", 2),
        "profit_target_pct": cfg.get("vrp_spreads", {}).get("profit_target_pct", 0.50),
        "stop_loss_multiplier": cfg.get("vrp_spreads", {}).get("stop_loss_multiplier", 2.0),
        "max_open_total": cfg.get("positions", {}).get("max_open_total", 3),
    })


# ── Dashboard HTML ────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>WheelBot Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script>
tailwind.config = {
  theme: {
    extend: {
      colors: {
        surface: '#111827',
        card: '#1f2937',
        'card-hover': '#374151',
        accent: '#10b981',
        'accent-dim': '#065f46',
        danger: '#ef4444',
        'danger-dim': '#7f1d1d',
        warn: '#f59e0b',
        info: '#3b82f6',
      }
    }
  }
}
</script>
<style>
  * { -webkit-tap-highlight-color: transparent; }
  body { background: #030712; }
  .shimmer { animation: shimmer 2s infinite; }
  @keyframes shimmer {
    0%,100% { opacity: 0.5; }
    50% { opacity: 1; }
  }
  .expand-enter { max-height: 0; overflow: hidden; transition: max-height 0.3s ease-out; }
  .expand-active { max-height: 500px; }
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #374151; border-radius: 2px; }
  .toggle-track { transition: background-color 0.2s; }
  .toggle-thumb { transition: transform 0.2s; }
  input[type=number] { -moz-appearance: textfield; }
  input::-webkit-outer-spin-button, input::-webkit-inner-spin-button { -webkit-appearance: none; }
</style>
</head>
<body class="text-gray-100 min-h-screen pb-8">

<!-- Header -->
<header class="sticky top-0 z-50 bg-gray-950/80 backdrop-blur-lg border-b border-gray-800 px-4 py-3">
  <div class="max-w-2xl mx-auto flex items-center justify-between">
    <div class="flex items-center gap-2">
      <span class="text-xl">&#x1F916;</span>
      <h1 class="text-lg font-bold tracking-tight">WheelBot</h1>
    </div>
    <div id="status-badge" class="flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full bg-gray-800">
      <span id="status-dot" class="w-2 h-2 rounded-full bg-gray-500 shimmer"></span>
      <span id="status-text">Loading...</span>
    </div>
  </div>
</header>

<main class="max-w-2xl mx-auto px-4 pt-4 space-y-4">

  <!-- Account Summary -->
  <section id="account-card" class="bg-card rounded-2xl p-5 space-y-4">
    <div class="text-center">
      <p class="text-xs text-gray-400 uppercase tracking-wider mb-1">Portfolio Value</p>
      <p id="portfolio-value" class="text-4xl font-bold tracking-tight text-white">--</p>
      <p id="day-pnl" class="text-sm mt-1 text-gray-400">Day P&amp;L: --</p>
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div class="bg-surface rounded-xl p-3 text-center">
        <p class="text-[10px] text-gray-500 uppercase tracking-wider">Buying Power</p>
        <p id="buying-power" class="text-lg font-semibold text-white mt-0.5">--</p>
      </div>
      <div class="bg-surface rounded-xl p-3 text-center">
        <p class="text-[10px] text-gray-500 uppercase tracking-wider">Cash</p>
        <p id="cash-balance" class="text-lg font-semibold text-white mt-0.5">--</p>
      </div>
    </div>
  </section>

  <!-- Open Positions -->
  <section>
    <div class="flex items-center justify-between mb-2">
      <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider">Open Positions</h2>
      <span id="pos-count" class="text-xs text-gray-500">0</span>
    </div>
    <div id="positions-list" class="space-y-2">
      <div class="bg-card rounded-xl p-4 text-center text-sm text-gray-500">No open positions</div>
    </div>
  </section>

  <!-- Recent Activity -->
  <section>
    <div class="flex items-center justify-between mb-2">
      <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider">Recent Activity</h2>
    </div>
    <div id="activity-list" class="space-y-2">
      <div class="bg-card rounded-xl p-4 text-center text-sm text-gray-500">No recent activity</div>
    </div>
  </section>

  <!-- Performance -->
  <section id="perf-section">
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-2">Performance</h2>
    <div id="perf-grid" class="grid grid-cols-2 gap-2">
      <div class="bg-card rounded-xl p-4 text-center text-sm text-gray-500 col-span-2">No data yet</div>
    </div>
  </section>

  <!-- Chart -->
  <section>
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-2">Portfolio History</h2>
    <div class="bg-card rounded-2xl p-4">
      <div id="chart-empty" class="text-center text-sm text-gray-500 py-8">
        &#x1F4C8; Will populate after first trading day
      </div>
      <canvas id="portfolio-chart" class="hidden w-full" height="200"></canvas>
    </div>
  </section>

  <!-- Settings -->
  <section>
    <button id="settings-toggle" class="w-full flex items-center justify-between bg-card rounded-2xl px-5 py-3 text-sm font-semibold text-gray-300 hover:bg-card-hover transition-colors">
      <span>&#x2699;&#xFE0F; Settings</span>
      <svg id="settings-chevron" class="w-4 h-4 text-gray-500 transition-transform" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>
    </button>
    <div id="settings-panel" class="expand-enter mt-2">
      <div class="bg-card rounded-2xl p-5 space-y-4">

        <!-- Toggles -->
        <div class="space-y-3">
          <div class="flex items-center justify-between">
            <div>
              <p class="text-sm font-medium">Paper Trading</p>
              <p class="text-[10px] text-gray-500">Use simulated account</p>
            </div>
            <label class="relative inline-flex cursor-pointer">
              <input type="checkbox" id="cfg-paper" class="sr-only peer" checked>
              <div class="toggle-track w-10 h-5 bg-gray-600 rounded-full peer-checked:bg-accent"></div>
              <div class="toggle-thumb absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full peer-checked:translate-x-5"></div>
            </label>
          </div>
          <div class="flex items-center justify-between">
            <div>
              <p class="text-sm font-medium">VRP Spreads</p>
              <p class="text-[10px] text-gray-500">Put credit spreads on SPY</p>
            </div>
            <label class="relative inline-flex cursor-pointer">
              <input type="checkbox" id="cfg-vrp" class="sr-only peer">
              <div class="toggle-track w-10 h-5 bg-gray-600 rounded-full peer-checked:bg-accent"></div>
              <div class="toggle-thumb absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full peer-checked:translate-x-5"></div>
            </label>
          </div>
          <div class="flex items-center justify-between">
            <div>
              <p class="text-sm font-medium">PMCC Strategy</p>
              <p class="text-[10px] text-gray-500">Poor man's covered calls</p>
            </div>
            <label class="relative inline-flex cursor-pointer">
              <input type="checkbox" id="cfg-pmcc" class="sr-only peer">
              <div class="toggle-track w-10 h-5 bg-gray-600 rounded-full peer-checked:bg-accent"></div>
              <div class="toggle-thumb absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full peer-checked:translate-x-5"></div>
            </label>
          </div>
          <div class="flex items-center justify-between">
            <div>
              <p class="text-sm font-medium">Wheel Strategy</p>
              <p class="text-[10px] text-gray-500">CSP + covered calls cycle</p>
            </div>
            <label class="relative inline-flex cursor-pointer">
              <input type="checkbox" id="cfg-wheel" class="sr-only peer">
              <div class="toggle-track w-10 h-5 bg-gray-600 rounded-full peer-checked:bg-accent"></div>
              <div class="toggle-thumb absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full peer-checked:translate-x-5"></div>
            </label>
          </div>
        </div>

        <hr class="border-gray-700">

        <!-- Number inputs -->
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="text-[10px] text-gray-500 uppercase tracking-wider">Max Positions</label>
            <input id="cfg-max-positions" type="number" min="1" max="10" value="3"
              class="w-full mt-1 bg-surface border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-accent">
          </div>
          <div>
            <label class="text-[10px] text-gray-500 uppercase tracking-wider">Max Spreads</label>
            <input id="cfg-max-spreads" type="number" min="1" max="10" value="2"
              class="w-full mt-1 bg-surface border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-accent">
          </div>
          <div>
            <label class="text-[10px] text-gray-500 uppercase tracking-wider">Profit Target %</label>
            <input id="cfg-profit" type="number" min="0.1" max="1" step="0.05" value="0.50"
              class="w-full mt-1 bg-surface border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-accent">
          </div>
          <div>
            <label class="text-[10px] text-gray-500 uppercase tracking-wider">Stop Loss x</label>
            <input id="cfg-stoploss" type="number" min="1" max="5" step="0.5" value="2.0"
              class="w-full mt-1 bg-surface border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-accent">
          </div>
        </div>

        <button id="save-settings" class="w-full bg-accent hover:bg-emerald-600 text-white font-semibold text-sm rounded-xl py-2.5 transition-colors">
          Save Settings
        </button>
        <p id="save-msg" class="text-center text-xs text-accent hidden">Settings saved</p>
      </div>
    </div>
  </section>

  <!-- Footer -->
  <footer class="text-center text-[10px] text-gray-600 pt-4 pb-2">
    WheelBot &middot; Auto-refreshes every 60s &middot; <span id="last-update">--</span>
  </footer>

</main>

<script>
// ── Helpers ─────────────────────────────────────────────────────────

function $(id) { return document.getElementById(id); }

function money(n) {
  return n == null ? '--' : '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function pnlColor(n) {
  return n > 0 ? 'text-emerald-400' : n < 0 ? 'text-red-400' : 'text-gray-400';
}

function pnlSign(n) { return n > 0 ? '+' : ''; }

function ts(iso) {
  if (!iso) return '--';
  var d = new Date(iso);
  return d.toLocaleDateString('en-US', {month:'short', day:'numeric'}) + ' ' + d.toLocaleTimeString('en-US', {hour:'numeric', minute:'2-digit'});
}

function escHtml(s) {
  if (!s) return '';
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(s));
  return div.innerHTML;
}

function strategyLabel(s) {
  var map = {
    'pmcc_leaps': 'PMCC LEAPS', 'pmcc_short_call': 'PMCC Short',
    'wheel_csp': 'Wheel CSP', 'wheel_cc': 'Wheel CC',
    'wheel_shares': 'Wheel Shares', 'vrp_spread': 'VRP Spread',
  };
  return map[s] || escHtml(s);
}

function actionEmoji(action) {
  var map = {
    'buy_leaps': '\uD83D\uDCB0', 'sell_short_call': '\uD83D\uDCC9',
    'sell_csp': '\uD83D\uDCC9', 'sell_cc': '\uD83D\uDCC9',
    'buy_to_close': '\u2705', 'roll': '\uD83D\uDD04',
    'close_pair': '\uD83D\uDD10',
  };
  return map[action] || '\u26A1';
}

function actionColor(action) {
  if (['buy_leaps', 'sell_csp', 'sell_cc', 'sell_short_call'].includes(action))
    return 'border-emerald-500/30 bg-emerald-500/5';
  if (['buy_to_close', 'close_pair'].includes(action))
    return 'border-amber-500/30 bg-amber-500/5';
  if (action === 'roll') return 'border-blue-500/30 bg-blue-500/5';
  return 'border-gray-700 bg-card';
}

function statusBadge(status) {
  var colors = {
    'pending': 'bg-yellow-500/20 text-yellow-400',
    'approved': 'bg-blue-500/20 text-blue-400',
    'executed': 'bg-emerald-500/20 text-emerald-400',
    'auto_executed': 'bg-emerald-500/20 text-emerald-400',
    'denied': 'bg-red-500/20 text-red-400',
    'expired': 'bg-gray-500/20 text-gray-400',
    'filled': 'bg-emerald-500/20 text-emerald-400',
    'cancelled': 'bg-gray-500/20 text-gray-400',
    'rejected': 'bg-red-500/20 text-red-400',
  };
  var cls = colors[status] || 'bg-gray-700 text-gray-300';
  var span = document.createElement('span');
  span.className = 'text-[10px] font-medium px-2 py-0.5 rounded-full ' + cls;
  span.textContent = status || '';
  return span.outerHTML;
}


// ── Safe DOM builders ───────────────────────────────────────────────

function buildPositionCard(p) {
  var pnl = p.pnl_dollars || 0;
  var pnlPct = p.pnl_percent || 0;
  var dte = p.dte_remaining != null ? p.dte_remaining + 'd' : '--';
  var borderColor = pnl >= 0 ? 'border-emerald-500/20' : 'border-red-500/20';
  var optType = p.option_type ? p.option_type.toUpperCase() : '--';
  var strikeStr = p.strike ? '$' + p.strike : '';

  var card = document.createElement('div');
  card.className = 'bg-card rounded-xl border ' + borderColor + ' overflow-hidden';

  var header = document.createElement('div');
  header.className = 'p-4 cursor-pointer';
  header.addEventListener('click', function() {
    var detail = this.nextElementSibling;
    detail.classList.toggle('expand-active');
  });

  var topRow = document.createElement('div');
  topRow.className = 'flex items-center justify-between';

  var leftSide = document.createElement('div');
  leftSide.className = 'flex items-center gap-2';

  var sym = document.createElement('span');
  sym.className = 'text-base font-bold text-white';
  sym.textContent = p.symbol || '';

  var strat = document.createElement('span');
  strat.className = 'text-[10px] font-medium px-1.5 py-0.5 rounded bg-gray-700 text-gray-300';
  strat.textContent = strategyLabel(p.strategy);

  leftSide.appendChild(sym);
  leftSide.appendChild(strat);

  var rightSide = document.createElement('div');
  rightSide.className = 'text-right';

  var pnlEl = document.createElement('p');
  pnlEl.className = 'text-sm font-semibold ' + pnlColor(pnl);
  pnlEl.textContent = pnlSign(pnl) + money(Math.abs(pnl));

  var pnlPctEl = document.createElement('p');
  pnlPctEl.className = 'text-[10px] ' + pnlColor(pnlPct);
  pnlPctEl.textContent = pnlSign(pnlPct) + pnlPct.toFixed(1) + '%';

  rightSide.appendChild(pnlEl);
  rightSide.appendChild(pnlPctEl);

  topRow.appendChild(leftSide);
  topRow.appendChild(rightSide);

  var meta = document.createElement('div');
  meta.className = 'flex items-center gap-3 mt-2 text-[10px] text-gray-500';
  meta.textContent = optType + ' ' + strikeStr + '  \u23F3 ' + dte + '  Exp ' + (p.expiration_date || '--');

  header.appendChild(topRow);
  header.appendChild(meta);

  var detail = document.createElement('div');
  detail.className = 'expand-enter border-t border-gray-700/50';

  var detailInner = document.createElement('div');
  detailInner.className = 'p-4 space-y-2 text-xs text-gray-400';

  var grid = document.createElement('div');
  grid.className = 'grid grid-cols-2 gap-2';

  var fields = [
    ['Entry', money(p.entry_price)],
    ['Current', money(p.current_price)],
    ['Delta', p.current_delta != null ? p.current_delta.toFixed(3) : '--'],
    ['Theta', p.current_theta != null ? p.current_theta.toFixed(3) : '--'],
    ['IV', p.current_iv != null ? (p.current_iv * 100).toFixed(1) + '%' : '--'],
    ['Qty', String(p.quantity)],
    ['Target', money(p.target_close_price)],
    ['Stop', money(p.stop_loss_price)],
  ];
  fields.forEach(function(f) {
    var div = document.createElement('div');
    div.textContent = f[0] + ': ';
    var val = document.createElement('span');
    val.className = f[0] === 'Target' ? 'text-emerald-400' : f[0] === 'Stop' ? 'text-red-400' : 'text-white';
    val.textContent = f[1];
    div.appendChild(val);
    grid.appendChild(div);
  });

  detailInner.appendChild(grid);

  if (p.ai_reasoning) {
    var aiNote = document.createElement('div');
    aiNote.className = 'mt-2 text-[10px] text-gray-500 italic';
    aiNote.textContent = p.ai_reasoning.substring(0, 200);
    detailInner.appendChild(aiNote);
  }

  detail.appendChild(detailInner);
  card.appendChild(header);
  card.appendChild(detail);
  return card;
}


function buildActivityItem(item) {
  var card = document.createElement('div');
  card.className = 'bg-card rounded-xl border ' + actionColor(item.action) + ' p-3';

  var topRow = document.createElement('div');
  topRow.className = 'flex items-center justify-between';

  var label = document.createElement('div');
  label.className = 'text-sm';
  if (item.type === 'signal') {
    label.textContent = actionEmoji(item.action) + ' ' + (item.symbol || '') + ' \u00B7 ' + (item.action || '').replace(/_/g, ' ');
  } else {
    label.textContent = '\uD83D\uDCCB Execution \u00B7 ' + (item.action || '');
  }

  topRow.appendChild(label);

  var badgeContainer = document.createElement('span');
  badgeContainer.innerHTML = statusBadge(item.status);
  topRow.appendChild(badgeContainer);

  var metaRow = document.createElement('div');
  metaRow.className = 'flex items-center gap-3 mt-1 text-[10px] text-gray-500';
  var parts = [];
  if (item.price) parts.push(money(item.price));
  if (item.strike) parts.push('$' + item.strike + ' strike');
  parts.push(ts(item.time));
  metaRow.textContent = parts.join(' \u00B7 ');

  card.appendChild(topRow);
  card.appendChild(metaRow);

  if (item.reason) {
    var reasonEl = document.createElement('p');
    reasonEl.className = 'text-[10px] text-gray-500 mt-1';
    reasonEl.textContent = item.reason;
    reasonEl.style.display = '-webkit-box';
    reasonEl.style.webkitLineClamp = '2';
    reasonEl.style.webkitBoxOrient = 'vertical';
    reasonEl.style.overflow = 'hidden';
    card.appendChild(reasonEl);
  }

  return card;
}


// ── Data loaders ────────────────────────────────────────────────────

async function loadAccount() {
  try {
    var r = await fetch('/api/account');
    var d = await r.json();

    $('portfolio-value').textContent = money(d.portfolio_value);

    var pnl = d.day_pnl || 0;
    var pnlEl = $('day-pnl');
    pnlEl.textContent = '';
    pnlEl.appendChild(document.createTextNode('Day P&L: '));
    var pnlSpan = document.createElement('span');
    pnlSpan.className = pnlColor(pnl);
    pnlSpan.textContent = pnlSign(pnl) + money(Math.abs(pnl));
    pnlEl.appendChild(pnlSpan);

    $('buying-power').textContent = money(d.buying_power);
    $('cash-balance').textContent = money(d.cash);

    var dot = $('status-dot');
    var txt = $('status-text');
    var badge = $('status-badge');
    if (d.status === 'running') {
      dot.className = 'w-2 h-2 rounded-full bg-emerald-400 shimmer';
      txt.textContent = d.mode === 'paper' ? 'Paper' : 'LIVE';
      badge.className = 'flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full ' +
        (d.mode === 'paper' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-red-500/10 text-red-400');
    } else if (d.status === 'error') {
      dot.className = 'w-2 h-2 rounded-full bg-red-400';
      txt.textContent = 'Error';
      badge.className = 'flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full bg-red-500/10 text-red-400';
    } else {
      dot.className = 'w-2 h-2 rounded-full bg-gray-500';
      txt.textContent = 'Offline';
      badge.className = 'flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full bg-gray-800';
    }
  } catch (e) {
    console.error('Account load failed:', e);
  }
}

async function loadPositions() {
  try {
    var r = await fetch('/api/positions');
    var positions = await r.json();
    var el = $('positions-list');
    $('pos-count').textContent = positions.length;

    if (positions.length === 0) {
      el.textContent = '';
      var empty = document.createElement('div');
      empty.className = 'bg-card rounded-xl p-4 text-center text-sm text-gray-500';
      empty.textContent = 'No open positions';
      el.appendChild(empty);
      return;
    }

    el.textContent = '';
    positions.forEach(function(p) {
      el.appendChild(buildPositionCard(p));
    });
  } catch (e) {
    console.error('Positions load failed:', e);
  }
}

async function loadActivity() {
  try {
    var results = await Promise.all([
      fetch('/api/signals'),
      fetch('/api/executions'),
    ]);
    var signals = await results[0].json();
    var executions = await results[1].json();

    var items = [];
    signals.slice(0, 10).forEach(function(s) {
      items.push({
        type: 'signal', symbol: s.symbol, action: s.action,
        strategy: s.strategy, status: s.status,
        price: s.limit_price || s.estimated_credit,
        strike: s.strike, option_type: s.option_type,
        reason: s.reason, time: s.created_at,
      });
    });
    executions.slice(0, 10).forEach(function(e) {
      items.push({
        type: 'execution', symbol: '', action: e.order_type,
        strategy: '', status: e.status,
        price: e.fill_price || e.requested_price,
        strike: null, option_type: '',
        reason: e.error_message, time: e.created_at,
      });
    });
    items.sort(function(a, b) { return (b.time || '').localeCompare(a.time || ''); });

    var el = $('activity-list');
    if (items.length === 0) {
      el.textContent = '';
      var empty = document.createElement('div');
      empty.className = 'bg-card rounded-xl p-4 text-center text-sm text-gray-500';
      empty.textContent = 'No recent activity';
      el.appendChild(empty);
      return;
    }

    el.textContent = '';
    items.slice(0, 10).forEach(function(item) {
      el.appendChild(buildActivityItem(item));
    });
  } catch (e) {
    console.error('Activity load failed:', e);
  }
}

async function loadPerformance() {
  try {
    var r = await fetch('/api/performance');
    var rows = await r.json();
    var grid = $('perf-grid');

    if (rows.length === 0) {
      grid.textContent = '';
      var empty = document.createElement('div');
      empty.className = 'bg-card rounded-xl p-4 text-center text-sm text-gray-500 col-span-2';
      empty.textContent = 'No data yet';
      grid.appendChild(empty);
      return;
    }

    var perf = rows.find(function(r) { return r.strategy === 'overall' && r.period === 'all_time'; }) || rows[0];

    var cells = [
      { label: 'Win Rate', value: (perf.win_rate * 100).toFixed(0) + '%', cls: perf.win_rate >= 0.5 ? 'text-emerald-400' : 'text-red-400' },
      { label: 'Total Trades', value: String(perf.total_trades), cls: 'text-white' },
      { label: 'Avg Profit', value: money(perf.avg_profit), cls: pnlColor(perf.avg_profit) },
      { label: 'Premium', value: money(perf.total_premium_collected), cls: 'text-emerald-400' },
      { label: 'Sharpe', value: perf.sharpe_ratio.toFixed(2), cls: 'text-white' },
      { label: 'Max DD', value: (perf.max_drawdown * 100).toFixed(1) + '%', cls: 'text-red-400' },
    ];

    grid.textContent = '';
    cells.forEach(function(c) {
      var cell = document.createElement('div');
      cell.className = 'bg-card rounded-xl p-3 text-center';

      var lbl = document.createElement('p');
      lbl.className = 'text-[10px] text-gray-500 uppercase';
      lbl.textContent = c.label;

      var val = document.createElement('p');
      val.className = 'text-xl font-bold ' + c.cls;
      val.textContent = c.value;

      cell.appendChild(lbl);
      cell.appendChild(val);
      grid.appendChild(cell);
    });
  } catch (e) {
    console.error('Performance load failed:', e);
  }
}

var chartInstance = null;

async function loadChart() {
  try {
    var r = await fetch('/api/snapshots');
    var snaps = await r.json();

    if (snaps.length === 0) {
      $('chart-empty').classList.remove('hidden');
      $('portfolio-chart').classList.add('hidden');
      return;
    }

    $('chart-empty').classList.add('hidden');
    var canvas = $('portfolio-chart');
    canvas.classList.remove('hidden');

    var labels = snaps.map(function(s) { return s.date; });
    var values = snaps.map(function(s) { return s.total_account_value; });

    if (chartInstance) chartInstance.destroy();

    var ctx = canvas.getContext('2d');
    var gradient = ctx.createLinearGradient(0, 0, 0, 200);
    gradient.addColorStop(0, 'rgba(16, 185, 129, 0.3)');
    gradient.addColorStop(1, 'rgba(16, 185, 129, 0)');

    chartInstance = new Chart(ctx, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: 'Portfolio Value',
          data: values,
          borderColor: '#10b981',
          backgroundColor: gradient,
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 4,
          fill: true,
          tension: 0.3,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#1f2937',
            titleColor: '#9ca3af',
            bodyColor: '#ffffff',
            borderColor: '#374151',
            borderWidth: 1,
            padding: 8,
            displayColors: false,
            callbacks: {
              label: function(ctx) { return money(ctx.parsed.y); }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: '#4b5563', font: { size: 10 }, maxRotation: 0, maxTicksLimit: 6 },
            border: { display: false },
          },
          y: {
            grid: { color: '#1f2937' },
            ticks: {
              color: '#4b5563',
              font: { size: 10 },
              callback: function(v) { return '$' + (v/1000).toFixed(1) + 'k'; }
            },
            border: { display: false },
          }
        },
        interaction: { intersect: false, mode: 'index' },
      }
    });
  } catch (e) {
    console.error('Chart load failed:', e);
  }
}

async function loadSettings() {
  try {
    var r = await fetch('/api/config');
    var cfg = await r.json();
    $('cfg-paper').checked = cfg.paper_trade;
    $('cfg-vrp').checked = cfg.vrp_enabled;
    $('cfg-pmcc').checked = cfg.pmcc_enabled;
    $('cfg-wheel').checked = cfg.wheel_enabled;
    $('cfg-max-positions').value = cfg.max_open_total;
    $('cfg-max-spreads').value = cfg.max_simultaneous;
    $('cfg-profit').value = cfg.profit_target_pct;
    $('cfg-stoploss').value = cfg.stop_loss_multiplier;
  } catch (e) {
    console.error('Config load failed:', e);
  }
}

async function saveSettings() {
  var body = {
    paper_trade: $('cfg-paper').checked,
    vrp_enabled: $('cfg-vrp').checked,
    pmcc_enabled: $('cfg-pmcc').checked,
    wheel_enabled: $('cfg-wheel').checked,
    max_open_total: parseInt($('cfg-max-positions').value),
    max_simultaneous: parseInt($('cfg-max-spreads').value),
    profit_target_pct: parseFloat($('cfg-profit').value),
    stop_loss_multiplier: parseFloat($('cfg-stoploss').value),
  };
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    var msg = $('save-msg');
    msg.classList.remove('hidden');
    setTimeout(function() { msg.classList.add('hidden'); }, 2000);
  } catch (e) {
    console.error('Save failed:', e);
  }
}

// ── Init & auto-refresh ─────────────────────────────────────────────

async function refreshAll() {
  await Promise.all([
    loadAccount(),
    loadPositions(),
    loadActivity(),
    loadPerformance(),
    loadChart(),
  ]);
  $('last-update').textContent = new Date().toLocaleTimeString('en-US', {hour:'numeric', minute:'2-digit'});
}

$('settings-toggle').addEventListener('click', function() {
  var panel = $('settings-panel');
  var chevron = $('settings-chevron');
  panel.classList.toggle('expand-active');
  chevron.style.transform = panel.classList.contains('expand-active') ? 'rotate(180deg)' : '';
});

$('save-settings').addEventListener('click', saveSettings);

refreshAll();
loadSettings();

setInterval(refreshAll, 60000);
</script>

</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard."""
    return DASHBOARD_HTML


# ── Run ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard:app", host="0.0.0.0", port=8888, reload=True)
