# WheelBot Audit Fix Task Board

## P0 — WILL LOSE MONEY (fix before going live)

- [ ] P0-1: Fix atomic spread orders — use Alpaca multi-leg API or place long leg FIRST at market, then short leg. NEVER leave a naked short.
- [ ] P0-2: Add buying power check before every order in executor.py
- [ ] P0-3: Fix VIX gate — fetch real VIX data, not VIXY ETF price
- [ ] P0-4: Add duplicate order protection — check if signal already executed before placing
- [ ] P0-5: Use market orders for stop-losses instead of limit orders

## P1 — WILL LOSE MONEY EVENTUALLY

- [ ] P1-6: Add max daily loss circuit breaker (halt trading at 5% daily loss)
- [ ] P1-7: Speed up exit engine to 2-minute intervals for positions near stop-loss
- [ ] P1-8: Add VRP spread reconciliation in reconciler.py
- [ ] P1-9: Fix live mode to create Position records when orders fill (not just paper mode)
- [ ] P1-10: Track both leg order IDs for spreads
- [ ] P1-11: Fix datetime.now() → now_et() in vrp_spreads.py line 106

## P2 — STRATEGY IMPROVEMENTS

- [ ] P2-12: Calculate real delta using Black-Scholes instead of hardcoding 0.16
- [ ] P2-13: Make spread width proportional to stock price (1-2% of price, min $1, max $10)
- [ ] P2-14: Add sector correlation check — prevent 2 spreads in same sector
- [ ] P2-15: Add IV Rank to scoring — only sell premium when IVR > 30
- [ ] P2-16: Fix scoring formula — normalize all components to 0-1 range

## P3 — CODE QUALITY

- [ ] P3-17: Fix TYPE_CHECKING imports from RobinhoodBroker → AlpacaBroker
- [ ] P3-18: Fix reconciler dict vs attribute access for AlpacaBroker returns
- [ ] P3-19: Fix reconciler key format mismatch (OCC symbols vs DB format)
- [ ] P3-20: Use parameterized column names in database.py update functions
- [ ] P3-21: Add daily SQLite backup + WAL mode checkpointing
