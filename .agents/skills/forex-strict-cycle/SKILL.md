---
name: forex-strict-cycle
description: How the FOREX 28-pair signal system is structured, how to run it, the strict 5-hour cycle filter, the free GitHub-Models AI workflows, and the conventions any AI assistant must follow when editing this repo. Read this skill at session start in any organization or account — it is the canonical project memory.
---

# FOREX Сигналы 2026 — operating manual for AI assistants

This file is the **single source of truth** for any AI assistant
working in this repo (Devin, Cursor, Copilot, etc.) — across
organizations and accounts.  Read it before doing anything else; the
knowledge here was painfully accumulated and should not be re-derived
from scratch every session.

## 1. What the system does

A FastAPI dashboard that displays real-time forex signals for **28
currency pairs**, plus a strict 5-hour forecast cycle, multi-broker
sanity check, and three GitHub-Actions-driven AI workflows that
self-tune the system.

- Live UI: `static/index.html` (auto-refresh every 10 s, all in
  Russian, optimised for Android Chrome).
- Source of price truth: **Yahoo Finance** via `yfinance` (`app/prices.py`).
  Other free sources (`ER-API`, `Frankfurter`) are only used in the
  multi-broker sanity check — never as a price input to the system.
- Signals only render at confidence ≥ 80 %.
- The strict 5-hour cycle is much tighter (see §3).

## 2. Repo layout

```
app/
├── config.py         # 28 pairs, UTC+5, thresholds
├── prices.py         # Yahoo Finance + cache
├── indicators.py     # 13 technical indicators
├── price_action.py   # Candlestick patterns
├── orderbook.py      # Bid/Ask, depth, S/R
├── analyzer.py       # 15 voting blocks, multi-TF scoring + is_strong_trend
├── cycle.py          # Strict 5h cycle: PREMIUM / STRONG / NORMAL tiers
└── main.py           # FastAPI server + background scanner
static/
└── index.html        # Single-page UI, embedded data on first paint
scripts/
├── cycle_5h.py            # 5h cycle runner (cron-driven)
├── multi_broker.py        # Yahoo-primary sanity check vs ER-API/Frankfurter
├── auto_tune.py           # Daily heuristic threshold tuner (no LLM)
├── ai_review.py           # AI strategy reviewer (GitHub Models, free)
├── ai_patcher.py          # AI code patcher — actually edits analyzer/cycle
├── ai_narrative.py        # AI written market narrative for Telegram
├── auto_fix_degraded.py   # Auto-blacklist losing pairs for 24 h
├── generate_pine.py       # Generate TradingView Pine strategies
```

(Other sections 3-8 unchanged — see git history.)

## 9. Testing the AI brain — concrete gotchas

Things I wish I'd known before testing PR #67.  All of these will
still be true unless someone deliberately refactors the surface.

### 9.1 Smoke run

```
python scripts/ai_brain.py --quick    # ~20 s, no external news fetch
python -m pytest tests/ -q            # 33 tests, <1 s
ruff check app/ tests/ scripts/
```

`--quick` writes `data/top1.json` (≈ 50 KB) — the canonical artefact
the SPA, Telegram bot, and any test should consume.  These are the
key paths you can `assert` on:

| Path | What to expect |
|---|---|
| `top1.pair`, `top1.side`, `top1.confidence` | a real pair or `null` (gate suppressed) |
| `top1.layers.big_players.score` | `int(round(clamp(base − quote, ±3)))` |
| `top1.layers.safety_5h.passes` | must be `True` if `top1` is not `null` |
| `top1.layers.safety_5h.projection.passes` | drift clipped to ±0.5 × ATR |
| `top1.layers.safety_5h.reversal.reversal` | `False` for the chosen pair |
| `top1.layers.senior_alignment.weekly_bias` | `'BUY'` / `'SELL'` / `None` |
| `big_players.components.cot` | per-currency CFTC z-scores in [-3, +3] |
| `favorite_check.ok` | True iff `top1` is not None |

If any are missing, the wiring in `scripts/ai_brain.py` (writer) or
`app/brain.py::select_top1` (computer) has regressed.

### 9.2 yfinance OHLC column casing — **uppercase**

When you hand-craft a DataFrame for `app.safety.reversal_risk_h1`,
`app.indicators`, etc., use `"Open"`, `"High"`, `"Low"`, `"Close"`,
`"Volume"` (capitalised).  Lowercase columns will raise `KeyError:
'Close'` deep inside the engulfing detector.  This matches what
`yfinance` returns, but is **not** what most pandas examples show.

```python
import pandas as pd
bars = pd.DataFrame({
    "Open":  [...], "High": [...], "Low": [...], "Close": [...],
    "Volume": [...],
})
```

### 9.3 Where to monkey-patch for `select_top1` tests

`app/brain.py` imports names from sibling modules at the top of the
file, so all of these live on `app.brain.<name>` and that's where
`unittest.mock.patch` must target — *not* the original module.

```python
patches = [
    patch("app.brain.fetch_macro_snapshot", return_value={}),
    patch("app.brain.currency_strength_from_macro", return_value=zero_ccy),
    patch("app.brain._sentiment_score", return_value={"score":0,"reason":"stub"}),
    patch("app.brain.political_risk_scores", return_value={"score":0,"reason":"stub","events":[]}),
    patch("app.brain.next_high_impact_events", return_value=[]),
    patch("app.brain.cot_currency_zscores", return_value=zero_ccy),
    patch("app.brain.big_player_scores", return_value=stub_bp),
    patch("app.brain.evaluate_pair", side_effect=fake_eval),
]
```

The stub `evaluate_pair` must return dicts with at least these keys
or `select_top1` will crash: `pair, side, confidence, score, reason,
veto, layers, price, levels`.

### 9.4 Dashboard shape — `/api/signals` returns a **dict**, not a list

```
GET /api/signals →
{
  "scan_count": N,
  "updated_at": "...",
  "pairs": {  "EURUSD": {...}, "GBPUSD": {...}, ... 28 keys ... }
}
```

The first background scan may return only ~18 pairs (Yahoo throttling
the parallel batch) — wait ~15 s for the second scan before asserting
length.  The headline UI counter says **"Всего пар: 28"** once all
scans complete.

### 9.5 Clear-favorite gate — can publish below 80 % confidence

The `CLEAR_FAVORITE_FLOOR = 80` and `CLEAR_FAVORITE_LEAD = 5`
constants are joined with **OR**, not AND — so `top1` is published
when *either* condition holds.  In practice this means the brain
can publish e.g. `USDJPY BUY 50 %` when its lead over Top-2 is 20
points, even though the SPA's old 80 % rule would have hidden the
signal.  If you want a strict 80 % minimum, raise the floor or
change `or` to `and` at `app/brain.py:514`.  Mention this to the
user before changing — they may want the lead-based escape.

### 9.6 Network in CI / sandbox

- CFTC `publicreporting.cftc.gov` is reachable from this sandbox and
  from GitHub Actions.
- `feeds.reuters.com` is **blocked** in the Devin sandbox — the news
  layer degrades to a neutral score, the brain still publishes.  This
  is expected and not a bug.
- `yfinance` direct curl returns 429 (rate limit) but the `yfinance`
  Python client succeeds because it uses a different endpoint and
  retries.  Don't switch to raw curl for testing.

### 9.7 Devin Secrets Needed

No secrets are required to run the brain or its tests.  Optional org
secrets that improve the workflows but are not required for testing:

- `CF_AI_API_TOKEN` / `CF_AI_ACCOUNT_ID` — Cloudflare Workers AI
  (free).  Without them the AI scripts use the GitHub Models fallback.
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — Telegram delivery.
  Without them the Telegram step is skipped.
