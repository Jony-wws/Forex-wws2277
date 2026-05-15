---
name: forex-strict-cycle
description: How the FOREX 28-pair signal system is structured, how to run it, the strict 5-hour cycle filter, the new 6-layer AI brain (Top-1 from 28), the free GitHub-Models AI workflows, the mobile red SPA in site/, and the conventions any AI assistant must follow when editing this repo. Read this skill at session start in any organization or account — it is the canonical project memory.
---

# FOREX Сигналы 2026 — operating manual for AI assistants

This file is the **single source of truth** for any AI assistant
working in this repo (Devin, Cursor, Copilot, etc.) — across
organizations and accounts. Read it before doing anything else; the
knowledge here was painfully accumulated and should not be re-derived
from scratch every session.

## 1. What the system does

Two coexisting subsystems share the same 28-pair data layer:

**A) Legacy FastAPI dashboard (3-5 forecasts per 5h cycle)** —
`app/main.py` + `static/index.html`, deployed to Fly.io. Runs the
15-block analyzer (`app/analyzer.py`), strict 5-hour cycle
(`app/cycle.py`), multi-broker sanity check, AI review/patcher.

**B) NEW AI brain + static mobile site (Top-1 from 28 per 5h cycle)** —
`app/brain.py` + `site/`, deployed to GitHub Pages. Runs a 6-layer
weighted scoring with hard veto gates, picks the single best pair,
and publishes JSON to the `data` branch which the static SPA reads.

Both use the same source of price truth: **Yahoo Finance** via
`yfinance` (`app/prices.py`). Never add simulators or fake data.
All UI in Russian. UTC+5 timezone.

## 2. Repo layout

```
app/
├── config.py         # 28 pairs, UTC+5, thresholds
├── prices.py         # Yahoo Finance + cache
├── indicators.py     # 13 technical indicators
├── price_action.py   # Candlestick patterns
├── orderbook.py      # Bid/Ask, depth, S/R
├── analyzer.py       # 15 voting blocks, multi-TF scoring + is_strong_trend
├── cycle.py          # Legacy strict 5h cycle: PREMIUM / STRONG / NORMAL tiers
├── brain.py          # NEW — 6-layer Top-1 scoring + hard veto
├── smc.py            # NEW — Smart Money Concepts (BOS/CHoCH, Order Blocks, FVG, Liquidity Sweeps)
├── wyckoff.py        # NEW — Wyckoff phase detection (accumulation/markup/distribution/markdown)
├── volume_profile.py # NEW — POC + Value Area High/Low
├── macro.py          # NEW — DXY/yields/gold/oil/VIX → currency strength
├── news_brain.py     # NEW — ForexFactory + Reuters/BBC RSS aggregation
└── main.py           # FastAPI server + background scanner
static/
└── index.html        # Legacy SPA (Fly.io)
site/
├── index.html        # NEW mobile red SPA (GitHub Pages)
├── app.js            # NEW frontend logic (vanilla JS, TradingView Advanced Chart Widget)
└── .nojekyll         # Prevents Jekyll processing on Pages
scripts/
├── ai_brain.py            # NEW — runs select_top1() across 28 pairs, writes top1.json + brain_full.json
├── cycle_5h.py            # Legacy 5h cycle runner
├── multi_broker.py        # Yahoo-primary sanity check vs ER-API/Frankfurter
├── auto_tune.py           # Daily heuristic threshold tuner
├── ai_review.py           # AI strategy reviewer (GitHub Models, free)
├── ai_patcher.py          # AI code patcher
├── ai_narrative.py        # AI written market narrative
└── ... (others)
.github/workflows/
├── ai_brain.yml           # NEW — full Top-1 cycle every 5h + quick refresh every 5min
├── gh_pages.yml           # NEW — publishes site/ directly (no Vite build)
├── refresh_data.yml       # Every 5min — signals/bars; overlay publish (preserves top1.json)
├── cycle_5h.yml           # Legacy 5h cron — generates state/forecasts.json
└── ... (others)
tests/
└── test_ai_brain.py       # 12 unit tests covering SMC/Wyckoff/VP/macro/news
```

## 3. Legacy strict 5-hour cycle (FastAPI / Fly.io)

Cron boundaries: `5 19,0,5,10,15 * * *` UTC (00:05, 05:05, 10:05,
15:05, 20:05 UTC). At each boundary the system:

1. Pulls fresh data for all 28 pairs.
2. Runs the multi-TF analyzer (D1 + H4 + H1 + M15) → 15 voting blocks.
3. Computes `is_strong_trend` flag — passes ALL these conditions:
   ```
   confidence ≥ STRONG_CONFIDENCE  (default 88)
   score / max_score ≥ STRONG_RATIO (default 0.55)
   multi_tf_aligned == True (D1 + H4 + H1 + M15 all in one direction)
   adx_h1 ≥ STRONG_ADX_H1   (default 25)
   adx_h4 ≥ STRONG_ADX_H4   (default 20)
   trend_persistence_5h ≥ STRONG_PERSISTENCE  (default 80, ≥4/5 H1 bars)
   ```
4. Picks **always 3-5 forecasts** (`MIN_PICKS=3`, `MAX_PICKS=5`).
5. Tiers: ★ PREMIUM, ⚡ STRONG, ⊙ NORMAL.
6. Writes `state/forecasts.json` and `reports/cycle_5h_latest.md`.

## 4. NEW 6-layer AI brain (Top-1 from 28)

`app/brain.py` exposes `select_top1(now)` which scores every pair across
six weighted layers, applies hard veto gates, and returns a single
best pair (or `None` if every pair is vetoed).

**Weights:**
| Layer | Weight | Source |
|---|---|---|
| Technical | 35% | analyzer.py (15 blocks) + SMC + Wyckoff + Volume Profile |
| Macro | 25% | DXY, US10Y/DE10Y/GBP10Y/JPY10Y, Gold, Brent, VIX (Yahoo) |
| Fundamental (Carry) | 15% | Central bank policy rate spread |
| News | 10% | ForexFactory RSS — high-impact in next 2h → veto |
| Sentiment | 10% | Risk-on/off pulse from DXY/VIX/Gold |
| Geopolitical | 5% | Reuters World + BBC World RSS, currency-tagged |

**Hard veto gates — any one fires → pair excluded:**
1. Multi-TF NOT aligned (D1+H4+H1+M15 not all same direction)
2. ADX H1 < 20 (no real trend)
3. High-impact news within next 120 min for base OR quote currency
4. Market closed (weekend gap)

If all 28 pairs are vetoed, `top1 = null` and the UI shows
«AI ждёт следующего цикла» — better to skip a cycle than force a bad trade.

**Cycle boundaries (UTC):** `0 19,0,5,10,15 * * *` (= UTC+5 00:00, 05:00,
10:00, 15:00, 20:00). Cron is in `.github/workflows/ai_brain.yml`.

**Output JSON (data branch):**
- `data/top1.json` — compact: `{generated_at_utc, next_cycle_utc, top1, top5, macro_currency_strength, sentiment, political_risk}`. Read by the SPA.
- `data/brain_full.json` — all 28 evaluations with every layer detail (audit trail).
- `data/signals.json` — legacy 28-pair snapshot (refresh_data.yml, every 5min).
- `data/bars/{PAIR}-{TF}.json` — OHLC bars per pair/timeframe.

## 5. Mobile site (`site/`, GitHub Pages)

Pure HTML+CSS+JS, no build step. `site/.nojekyll` prevents Jekyll.
Fetches data from `https://cdn.jsdelivr.net/gh/Jony-wws/Forex-wws2277@data/data/*.json` with cache-busting by minute.

**Color palette:** `--bg #14080a`, `--card #1a0a0c`, `--primary #e53935`, `--accent #ff5252`, `--text #f6e3df`. Dark red, mobile-first, optimised for Android Chrome.

**Tabs (all in Russian):** Главная · 28 пар · Анализ · Новости · Сила · Журнал. DOM ids: `#tab-home`, `#tab-pairs`, `#tab-analysis`, `#tab-news`, `#tab-strength`, `#tab-journal`. Countdown: `#countdown`. Hero TradingView mount point: `#heroChart`.

**TradingView widgets:** Advanced Chart Widget (free, no account). Symbol format: `FX:EURUSD`. Timezone: `Asia/Karachi` (UTC+5). Theme: `dark`.

**Live URL:** https://jony-wws.github.io/Forex-wws2277/

## 6. Testing the live site (Devin testing mode)

For any change touching `app/brain.py`, `site/**`, or any of the new modules, validate against the deployed Pages site:

1. Check CI green: `git_pr_checks` for the PR, look for "Deploy GitHub Pages" + "AI brain (Top-1 cycle)" success.
2. Verify `data` branch has fresh JSON: `git show origin/data:data/top1.json | python3 -m json.tool | head -20` — should show non-null `top1` (unless all 28 vetoed).
3. Open https://jony-wws.github.io/Forex-wws2277/ in Chrome. Assert:
   - Hero card shows `top1.pair`, `top1.side`, `top1.confidence%`, `top1.levels.entry/stop_loss/take_profit` matching the JSON.
   - Countdown is NOT `—:—:—` and decrements between two screenshots taken ≥5s apart.
   - 6 layer rows visible: T, M, F, N, S, P — all reasons in Russian.
   - TradingView iframe loads for the hero pair (candles visible, not blank).
   - Tabs `28 пар`, `Анализ`, `Новости`, `Сила`, `Журнал` switch on click and render data.
   - `Сила` tab shows 8 currencies with macro scores (not all zero — that would mean macro fetch is broken).
4. If the user says «без видео / no video», take screenshots only. Do not start a recording.
5. Post ONE GitHub PR comment with collapsible `<details>` sections and the screenshot evidence (Top-1 + countdown delta is the headline proof).

## 7. Critical invariants — never break

- **28 pairs** in `app/config.py::PAIRS` — never reduce.
- **15 voting blocks** in `app/analyzer.py` — only weights/thresholds may change.
- **5-hour cycle frequency** — never change.
- **Only Yahoo Finance / public RSS** for live data. Never add simulators or fake data.
- **All UI in Russian.** UTC+5 user timezone.
- **Legacy `MIN_PICKS = 3`** for the `cycle.py` system (3-5 forecasts).
- **NEW brain.py** returns Top-1 (single pair) or null. Do not force a Top-1 if all 28 are vetoed.
- **Hard veto gates** in `_veto_check()` are safety-critical for real-money trading — do not weaken them without explicit user approval.
- **Mobile-first** — site/ must render correctly on Android Chrome.
- **Disclaimer** on Главная: «информационный характер, не инвест-рекомендация» — must remain visible.

## 8. Free AI on GitHub Actions — uses `GITHUB_TOKEN`, no paid keys

Three LLM-powered workflows run on GitHub-hosted runners using the
free GitHub Models API (no paid keys). They read state/forecasts.json
and reports/, then either commit a code patch PR (`ai_patcher.yml`),
write a strategy review (`ai_review.yml`), or generate a market narrative for Telegram (`ai_narrative.yml`).

## 9. Where data lives

- **`main` branch** — source code (apps, scripts, site/, workflows).
- **`data` branch** — orphan branch, filled by cron jobs. Contains `top1.json`, `brain_full.json`, `signals.json`, `bars/*.json`. The static SPA fetches from here via jsdelivr.
- **`state/` directory on main** — gitignored runtime state for the legacy FastAPI server (forecasts.json etc.). NOT used by the new brain.
- **`reports/` directory on main** — committed, generated by legacy `cycle_5h.py` and AI workflows.

## 10. Useful commands

```bash
# Run the new brain locally (no API keys needed, public RSS + Yahoo)
python scripts/ai_brain.py
# → writes data/top1.json + data/brain_full.json

# Quick refresh (no full brain, just signals/bars)
python scripts/ai_brain.py --quick

# Unit tests for the new brain
python -m pytest tests/test_ai_brain.py -q
# Expected: 12 passed

# Inspect Top-1 from the data branch (live production)
git fetch origin data --depth=1
git show origin/data:data/top1.json | python3 -m json.tool | head -40

# Manually trigger the brain workflow (requires GitHub auth)
gh workflow run ai_brain.yml
```

## 11. Devin Secrets Needed

For full testing of the live site:
- **None** — site is public, no auth required.

For running AI workflows locally:
- `GITHUB_TOKEN` — already provided by GitHub Actions runtime, not needed locally.
- All RSS feeds (ForexFactory, Reuters, BBC) and Yahoo Finance are public.

For deploying changes:
- Standard git push auth (handled by Devin's git proxy).
