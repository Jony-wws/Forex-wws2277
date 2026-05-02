# Deriv v17 PRO Trading Bot

Autonomous binary-options trading bot for Deriv demo account.

## Components

- **deriv_v17_pro.py** — main bot orchestrator (loads rules, applies filters, opens trades)
- **edge_v14.py** — v14 score calculation (MACD divergence, Supertrend, Pivots, OBV)
- **news_filter.py** — Forex Factory free calendar feed (high-impact event blackout)
- **v16_rules.json** — 25 per-(pair, session) rules backtested at 76.5% aggregate WR

## Filters applied (pro-trader logic)

1. v16 rule match (per pair + session, with score / vc / oc thresholds)
2. **News blackout**: 60 min before / 30 min after high-impact events
3. **Volatility regime**: skip if ATR > 2× 30-day median
4. **Trend alignment**: 4H EMA-20 slope must agree with signal direction
5. **Liquidity warmup**: skip first 30 min after session open
6. **Risk**: max 5 parallel, 4-hour dedupe per (pair, session)

## Run once

```bash
DERIV_DEMO_TOKEN=xxx ./run_once.sh
```

Scheduled to run every 15 minutes on a Devin scheduled session.
