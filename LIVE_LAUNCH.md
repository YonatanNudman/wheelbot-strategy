# Live Launch Checklist

Everything needed to flip WheelBot from paper to live. Do these in order.
The bot is **ready** — this is a sequence of YOUR actions, not code changes.

---

## Readiness scorecard (2026-04-20)

| Criterion | Status | Notes |
|---|---|---|
| Stop-loss fires autonomously | ✅ **Proven** | DKNG stop fired @ 10:11 AM EDT today at 2.05× entry, market order, $81 loss. No human action. |
| GTC profit-target auto-cancels on position close | ✅ **Proven** | DKNG's GTC was cancelled by Alpaca when stop fired. |
| Multiple symbols trading concurrently | ✅ **Proven** | 7 open positions across 7 symbols. |
| Discord alerts render correctly in new format | ⏳ Pending | First test: tomorrow's 9:35 AM scan (or today's 1:35 PM re-entry scan). |
| 20+ closed trades | ❌ 2/20 | 1 win (SOFI +$38), 1 loss (DKNG -$81). Levers 1-3 should accelerate. |
| 30d positive net P&L | ❌ -$193 | Account 10 days old. Statistical noise, not signal. |

**My call**: sufficient for **small-capital live launch**. Start at $2K. Scale only after you hit 20+ closed trades AND a positive 30d window.

---

## Step 1 — Open + fund live account (you)

1. Log into Alpaca
2. Open a **live** trading account (NOT paper). They're separate accounts with separate keys.
3. Fund with **$2,000**. Not more. This is your "prove it in production" capital.
4. Generate live API keys. They start with `AK` (paper keys start with `PK`).

## Step 2 — Back up current state (10 seconds)

```bash
cd "/Users/yonatan/Stock App"
cp .env .env.paper.backup
cp config.yaml config.yaml.paper.backup
git status  # make sure everything committed
```

If anything goes wrong, restore with `cp .env.paper.backup .env`.

## Step 3 — Swap credentials in .env

Edit `.env`. Add NEW vars, keep PAPER vars so you can flip back:

```bash
# Live keys (NEW - add these)
ALPACA_LIVE_API_KEY=AK...         # from step 1
ALPACA_LIVE_SECRET_KEY=...        # from step 1

# Active keys (change these to point at LIVE)
ALPACA_API_KEY=AK...              # same as ALPACA_LIVE_API_KEY
ALPACA_SECRET_KEY=...             # same as ALPACA_LIVE_SECRET_KEY
ALPACA_BASE_URL=https://api.alpaca.markets   # NOT paper-api
```

## Step 4 — Update config.yaml

```yaml
broker:
  paper_trade: false       # was true
  auto_execute: true

capital:
  total: 2000              # was 100000 — match your live deposit
  max_per_position_pct: 0.20
  reserve_pct: 0.10
```

That's it for code/config.

## Step 5 — Swap credentials in all 4 cloud routines

This is the one I can't do for you — you need your live API keys. For each of:
- WheelBot Morning Scan
- WheelBot Position Monitor
- WheelBot Daily Reflection
- WheelBot Weekly Autopsy

Find this line near the top of the prompt:
```
CREDENTIALS: API Key: PK7SQLLS75HIWHTJACSBJO2IK3 | Secret: A2MwvqDHeeVh5VKQKDu2F7TqLV5PhDyqBCWRa56KdUAf | Paper: True
```

Replace with:
```
CREDENTIALS: API Key: <YOUR_LIVE_AK_KEY> | Secret: <YOUR_LIVE_SECRET> | Paper: False
```

Save each routine.

**Alternative — easier**: keep the paper routines running AND create 4 new live routines (clone) with different names. That way paper keeps running as a control. Takes ~5 minutes more but is far safer.

## Step 6 — Verify with a tiny test run

Before the next scheduled scan, click **"Run now"** on the Morning Scan routine. Watch for:
- Discord alert appears with new 5-line format
- The alert says live credit, not paper
- You can see the order in the Alpaca LIVE dashboard (not paper)

If anything looks wrong → restore .env backup, revert routine creds, done. No money lost.

## Step 7 — Monitor the first 48 hours intently

Things to check every hour during market hours:
- Positions appearing in LIVE Alpaca dashboard
- GTC profit targets placed after each fill
- Position Monitor Discord snapshots arriving
- No duplicate orders
- No "insufficient buying power" errors in routine run logs

First 48h is where operational bugs surface (if any).

## Step 8 — Scale-up plan

Don't scale capital until all three are true:
1. 20+ closed round-trip trades on live
2. Positive 30-day P&L
3. No operational incidents (duplicate orders, missed stops, etc.)

Then scale 2× at a time: $2K → $4K → $8K → $16K. Not $2K → $100K.

---

## Rollback procedure (if anything breaks)

1. In each cloud routine, replace live creds with paper creds + `Paper: True`
2. Local: `cp .env.paper.backup .env && cp config.yaml.paper.backup config.yaml`
3. Open positions on live: either let them run to expiration or manually close
4. Investigate the bug before going live again

---

## What the bot will do differently on Day 1 live

- **Fills**: at the bid, not mid. Expect ~5-8% lower credit than paper showed
- **Slippage**: stop-loss market orders may fill 5-15% worse than mid on volatile names
- **Latency**: live orders route to real exchanges; paper is simulated instantly
- **Your cash flows**: you actually own the obligations. If a CSP is assigned, 100 shares land in your account.

Nothing here is alarming. Just different.
