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
└── backtest_*.py          # Backtests
.github/workflows/
├── cycle_5h.yml           # 5h cron — generates state/forecasts.json
├── multi_broker.yml       # Every 30 min — sanity check
├── ai_review.yml          # 12 min after each cycle — heuristic + LLM review
├── ai_patcher.yml         # Daily — LLM writes a code patch PR
├── ai_narrative.yml       # After each cycle — Telegram narrative
├── auto_tune.yml          # Daily — heuristic threshold PR
├── backtest.yml           # On every push touching analyzer/config
└── (others)
state/                     # gitignored runtime state — forecasts.json, etc.
reports/                   # Generated reports (committed)
```

## 3. Strict 5-hour cycle — the heart of the system

Cron boundaries: `5 19,0,5,10,15 * * *` UTC (00:05, 05:05, 10:05,
15:05, 20:05 UTC).  At each boundary the system:

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
4. Picks **always 3-5 forecasts** (`MIN_PICKS=3`, `MAX_PICKS=5`).  If
   fewer than 3 pairs passed the gate, the slate is topped up with
   the best remaining candidates by composite score.
5. Tiers: ★ PREMIUM (gate + ADX H1 ≥ 28 + persistence = 100 %), ⚡ STRONG
   (gate), ⊙ NORMAL (top-up).
6. Writes `state/forecasts.json` and `reports/cycle_5h_latest.md`.

**Critical invariants — never break:**
- `MIN_PICKS = 3` — user explicitly requires ≥ 3 forecasts every 5 h.
- 28 pairs in `app/config.py::PAIRS` — never reduce.
- 15 voting blocks in `app/analyzer.py` — only weights/thresholds may change.
- 5-hour cycle frequency — never change.
- Only Yahoo Finance for live price.  Never add simulators or fake data.

## 4. Free AI on GitHub Actions — uses `GITHUB_TOKEN`, no paid keys

Three workflows, all **free**, all using `https://models.github.ai/inference`
through the auto-injected `GITHUB_TOKEN`.  Default model is
`openai/gpt-4o-mini` (overridable via `GITHUB_MODEL` env in the workflow).

| Workflow | When | What it does |
|---|---|---|
| `ai_review.yml` | 12 min after each cycle + cron fallback | Reads cycle report and produces critical review with parameter suggestions in `reports/ai_review_latest.md`.  Heuristic mode always runs in parallel as a safety net. |
| `ai_patcher.yml` | Daily 04:00 UTC | Reads recent WR + the source of `analyzer/cycle/config`, asks the model to write actual code patches as JSON, applies them with safety checks (max 8 changes, ≤ 200 lines, smoke-compile), and opens a PR for human review. |
| `auto_tune.yml`  | Daily 03:30 UTC | Pure-Python heuristic — bumps `STRONG_*` thresholds one notch up if WR < 55 %, one notch down if WR > 75 %.  No LLM. |
| `ai_narrative.yml` | After each cycle | LLM writes a short human-friendly market narrative + Telegram delivery (if Telegram secrets are configured). |

**Do not** add `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` unless the user
explicitly asks — they are paid.  The system already works free.

### 4.1 Cloudflare Workers AI — primary model (free Llama 3.3 70B)

`scripts/ai_patcher.py`, `scripts/ai_review.py` and `scripts/ai_narrative.py`
now call **Cloudflare Workers AI** (`@cf/meta/llama-3.3-70b-instruct-fp8-fast`)
as the **primary** model and fall back transparently to GitHub Models
(`openai/gpt-4o-mini`) when the Cloudflare secrets are missing or the
call fails — the scripts never crash. Endpoint:
`https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}`
with `Authorization: Bearer {api_token}`. To enable Cloudflare, add two
repository secrets in *Settings → Secrets and variables → Actions*:
**`CF_AI_API_TOKEN`** — create a token with the *Workers AI* template at
<https://dash.cloudflare.com/profile/api-tokens>; and **`CF_AI_ACCOUNT_ID`** —
the 32-character hex ID visible at <https://dash.cloudflare.com/?to=/:account/ai>
(also in the URL of the AI dashboard). Optional override:
`CF_AI_MODEL` env var (default `@cf/meta/llama-3.3-70b-instruct-fp8-fast`).
Cloudflare Workers AI has a generous free daily quota — far above our
~5 cycles/day usage. If the token is later removed, the workflows keep
working unchanged via the GitHub Models fallback.

## 5. Quick start (local dev)

```bash
cd ~/repos/Forex-wws2277
pip install -q fastapi uvicorn yfinance pandas numpy slowapi
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Smoke check: `curl -s http://127.0.0.1:8080/api/signals | python3 -c "import sys,json; print(len(json.load(sys.stdin)['pairs']),'pairs')"` (should print `28 pairs`).

For public access during a session, use `deploy expose port=8080`.
For long-lived deployment, `deploy backend` to Fly.io.

## 6. PR conventions

- Branch name: `devin/<unix-ts>-<slug>`.
- Always create a PR — never push directly to `main`.
- After CI is green, summarise to the user with the PR link.
- Prefer small focused PRs over large refactors.

## 7. Forbidden / dangerous edits

- Removing pairs from `app/config.py::PAIRS`.
- Removing or renaming voting blocks in `app/analyzer.py` (only
  weights/thresholds may change).
- Lowering `MIN_PICKS` below 3.
- Changing the cron frequency away from 5 h.
- Calling external APIs other than Yahoo / ECB / GitHub Models /
  HuggingFace / Telegram from any script in `scripts/`.
- Adding `eval`, `exec`, `subprocess`, `import os` to anything the AI
  patcher might generate (these are blocked by the patcher's safety net,
  but humans should also avoid them).

## 8. Cross-organization continuity

This `.agents/skills/SKILL.md` is intentionally written so a fresh
Devin / Cursor / Copilot session in **any organization or account**
gains the same context just by cloning the repo.  When you switch orgs:

1. Make sure the new org has the GitHub repo cloned.
2. The new Devin session will auto-load this skill on session start.
3. Optionally, copy any session-specific knowledge notes manually
   (Devin webapp → Settings → Knowledge → Export / Import).
4. The free GitHub-Models AI works out of the box — `GITHUB_TOKEN` is
   provided automatically inside Actions, no per-org config needed.

The same applies to GitHub Actions: free-tier models access is
per-account, not per-Devin-org.  As long as the repo lives in an
account that has GitHub Models enabled, the workflows will work.

## 9. Useful one-liners

```bash
# Latest cycle WR
python -c "import json,pathlib; d=json.loads(pathlib.Path('state/forecasts.json').read_text()); print(d.get('rolling_wr_5h'))"

# Force a manual cycle locally
python scripts/cycle_5h.py --once

# Run heuristic AI review without any token
python scripts/ai_review.py

# Run AI patcher (requires GITHUB_TOKEN env var)
GITHUB_TOKEN=$(gh auth token) python scripts/ai_patcher.py
```
