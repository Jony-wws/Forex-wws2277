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
├── brain.py          # 7-layer Top-1 AI brain (TA + macro + big-players + …)
├── big_players.py    # Smart Money composite: COT + bid/ask + macro flow
├── cot.py            # CFTC Commitment of Traders (public Socrata API)
├── safety.py         # 5h projection, reversal-risk, W1 bias, M5 momentum
├── smc.py            # Smart Money Concepts (Order Blocks, FVG, BOS/CHoCH)
├── wyckoff.py        # Wyckoff phases on H4 / D1
├── volume_profile.py # POC / VAH / VAL value-area scoring
├── macro.py          # DXY / yields / commodities → currency strength
├── news_brain.py     # High-impact event veto + political risk
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
├── memory_index.py        # Daily — embed historical winners into Supabase pgvector
├── memory_query.py        # After each cycle — KNN historical analogs report
└── backtest_*.py          # Backtests
.github/workflows/
├── cycle_5h.yml           # 5h cron — generates state/forecasts.json
├── multi_broker.yml       # Every 30 min — sanity check
├── ai_review.yml          # 12 min after each cycle — heuristic + LLM review
├── ai_patcher.yml         # Daily — LLM writes a code patch PR
├── ai_narrative.yml       # After each cycle — Telegram narrative
├── auto_tune.yml          # Daily — heuristic threshold PR
├── backtest.yml           # On every push touching analyzer/config
├── memory_index.yml       # Daily 03:00 UTC — re-index historical winners
├── memory_query.yml       # After each cycle — refresh memory_neighbors_latest.md
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

### 3a. AI brain `select_top1()` — 8 layers + safety gates

`app/brain.py` runs a parallel "Top-1 of 28 with a clear favorite"
pipeline used by `scripts/ai_brain.py` and consumed by the SPA /
Telegram bot.  Rebalanced 2026-05-15 for the 5-hour binary horizon —
technicals and the new multi-TF/multi-indicator confluence dominate
because carry trade and political risk barely move 5 h prices.  The
composite confidence is the weighted sum:

| Layer            | Weight | Source                                            |
|------------------|--------|---------------------------------------------------|
| `technical`      | 0.30   | `analyzer.py` votes + SMC/Wyckoff/VP extras       |
| `confluence`     | 0.25   | `confluence.py` 5-TF + 10-indicator confluence    |
| `macro`          | 0.12   | DXY, yields, commodities (`macro.py`)             |
| `big_players`    | 0.08   | CFTC COT + bid/ask + macro (`big_players.py`)     |
| `fundamental`    | 0.07   | Carry / policy rate diff (`brain.py`)             |
| `news`           | 0.08   | High-impact event veto (`news_brain.py`)          |
| `sentiment`      | 0.05   | Risk-on / risk-off from VIX & DXY tape            |
| `political`      | 0.05   | Reuters / BBC headline risk score                 |

The `confluence` layer is built from `app/confluence.py` and
`app/extra_indicators.py` (MFI, CCI, OBV slope, Supertrend, Vortex,
ROC, Bollinger/Keltner squeeze, Donchian).  It produces a directional
score from 5 timeframes (W1 + D1 + H4 + H1 + M15) and 10 independent
indicator votes.  When all 5 TFs agree + ≥7/10 indicators agree +
ADX(H1) ≥ 22 + volatility expansion confirms, `super_confluence`
fires and brain awards a +0.18 `SUPER_CONFLUENCE_BONUS` to composite.
Combined with the existing `+0.22` STRONG_TREND_BONUS this lets a
technically-perfect setup clear the strict 80 % publication floor on
its own merits, without needing macro/carry to agree — addressing the
user's product ask "больше шансов найти настоящие 80 % в каждом цикле".

The `_scale_confidence` calibration anchors were also retuned to
match the new weights:
- `composite=0.00` → 0  %
- `composite=0.20` → 50 %
- `composite=0.45` → 80 % (publication floor)
- `composite=1.00` → 99 %

Hard gates promoted to `veto` so the pair is excluded from Top-1:

- **Multi-TF alignment** — D1 + H4 + H1 + M15 must agree (analyzer).
- **W1 bias** — weekly EMA-20 bias must not be against the trade.
- **M5 momentum** — last 6 M5 closes must not contradict the trade.
- **Reversal risk H1** — last 3 H1 bars must not show engulfing /
  shooting-star / hammer against the trade.
- **5-hour projection** — projected close in 5 H1 bars must stay in
  profit by at least 0.5 × ATR(H1).  This is the user-required
  "последний момент не должен быть минус" guard.
- **News veto** — no high-impact econ event within 120 min on either
  side of the pair.
- **Clear-favorite gate (strict 80 %)** — Top-1 is published ONLY when
  the leader's confidence is ≥ `CLEAR_FAVORITE_FLOOR` (= 80).  The
  lead over Top-2 is still reported inside `favorite_check` for
  telemetry but is NOT part of the decision.  Below 80 % the cycle
  publishes `top1=null` with the reason
  "Нет явного фаворита: Top-1 N% < порог 80%".

`scripts/ai_brain.py` writes `data/top1.json` (also `brain_full.json`
on the slow path) with `top1`, `top5`, `big_players`, `favorite_check`
plus the layer-by-layer breakdown for transparency.

The two GitHub Actions workflows that keep the `data` branch fresh
are now **minute-level** (despite GitHub cron's 5-min floor):

- `.github/workflows/ai_brain.yml` — outer cron `*/5 * * * *` (quick
  mode).  Each run loops **5 × 60 s**, calling
  `python scripts/ai_brain.py --quick` and publishing `top1.json` via
  `scripts/ci/data_publish.sh`.  The 5-hour boundary cron still runs
  the FULL brain (writes both `top1.json` and `brain_full.json`).
- `.github/workflows/refresh_data.yml` — outer cron `*/5 * * * *`.
  Each run loops **5 × 60 s**, calling
  `python scripts/build_static_data.py`.  Iter #1 includes the heavy
  `data/bars/*` regeneration (≈ 3–5 min); iters #2–5 pass
  `--no-bars` so only the small JSONs (`signals.json`, `cycle.json`,
  `orderbooks.json`, `health.json`) refresh at the 1-minute cadence.

`scripts/ci/data_publish.sh` is the single push helper used by both
workflows.  It clones the `data` branch into `$RUNNER_TEMP/data-clone`
(so the workspace stays on `main`), overlays the supplied files, and
retries push up to 5× with `git fetch + git reset --hard` between
attempts to absorb race conditions when both workflows push in the
same minute.

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

## 9. Telegram Mini App

The dashboard can be opened **inside Telegram** (no browser switch) via
a Mini App.  Three pieces make this work:

- `GET /tg` in `app/main.py` — same dashboard as `/`, but with the
  Telegram Web App SDK (`telegram-web-app.js`) and Telegram theme
  variables wired up.  Degrades gracefully in a normal browser.
- `scripts/telegram_bot.py` — tiny stdlib + `requests` long-poll bot.
  On `/start` it replies with one inline `web_app` button labelled
  "Открыть FOREX 28" pointing at the public `/tg` URL.  Reads
  `TELEGRAM_BOT_TOKEN` and `DASHBOARD_URL` from env.  No heavy deps —
  do **not** add `python-telegram-bot`.
- `.github/workflows/telegram_bot_keepalive.yml` — runs the bot for ~5
  minutes every 30 minutes (cron + `workflow_dispatch`) on the free
  tier.  Skips silently if `TELEGRAM_BOT_TOKEN` is unset.

### Setup checklist (one-time)

1. **BotFather → /newbot** → save the token as repo secret
   `TELEGRAM_BOT_TOKEN`.
2. Deploy the dashboard to a stable HTTPS URL (e.g. `deploy backend`
   to Fly.io, or any free host).  Add the **base** URL (no `/tg`) as
   repo secret `DASHBOARD_URL`.
3. **BotFather → /setmenubutton** → choose the bot → enter button text
   ("FOREX 28") and the URL `<DASHBOARD_URL>/tg`.  Telegram will then
   render a persistent Mini App button next to the chat input.
4. Optional: **/setdomain** the public origin so Telegram trusts
   `web_app` buttons from arbitrary chats.
5. Trigger the workflow once via the **Actions → Telegram bot
   keepalive → Run workflow** button to verify everything works, then
   let cron take over.

### Local manual test

```bash
TELEGRAM_BOT_TOKEN=... DASHBOARD_URL=https://example.com \
RUN_SECONDS=60 python scripts/telegram_bot.py
```

Then DM the bot `/start` — you should receive an inline button that
opens the dashboard inside Telegram.

### Constraints / forbidden edits

- `/tg` must stay a thin wrapper over `static/index.html` — never fork
  the page or duplicate the UI.
- Do not add heavy Telegram libraries; keep `scripts/telegram_bot.py`
  on the standard library + `requests` only.
- Do not change the `/` route's behaviour for non-Telegram clients.

## 10. Supabase pgvector — historical winning-setup memory

A free **vector-memory** module lives in `scripts/memory_index.py` and
`scripts/memory_query.py`.  It embeds every evaluated 5h forecast from
`state/forecasts.json` as a deterministic 9-D feature vector
(`confidence`, `score`, `score/max_score`, `adx_h1`, `adx_h4`,
`persistence`, `multi_tf_aligned`, `side`, `win/loss`) — **no LLM call
for embedding**, so it stays free and local.  It then asks Supabase
`pgvector` for the K=10 nearest historical setups by cosine distance,
and writes a markdown summary to `reports/memory_neighbors_latest.md`
(e.g. "Current EURUSD BUY setup matches 7/10 past winners").  Both
`scripts/ai_review.py` and `scripts/ai_patcher.py` read that file and
inject it into their LLM prompt so the model sees historical analogs.

Workflows:
- `.github/workflows/memory_index.yml` — daily 03:00 UTC re-index.
- `.github/workflows/memory_query.yml` — after each 5h cycle (with cron
  fallback `14 19,0,5,10,15 * * *`).

Both workflows skip silently when `SUPABASE_URL` / `SUPABASE_KEY`
secrets aren't set, so a fresh fork keeps green CI without any setup.

### One-time Supabase setup (free tier — 500 MB Postgres)

1. Create a free project at <https://supabase.com/dashboard/projects>
   (free tier, no credit card).
2. Project Settings → API → copy `Project URL` and `service_role` key.
3. Add them as repo secrets `SUPABASE_URL` and `SUPABASE_KEY`
   (Repo → Settings → Secrets and variables → Actions).
4. SQL editor → run the bootstrap SQL printed by
   `python scripts/memory_index.py` on first run, or paste:

   ```sql
   create extension if not exists vector;
   create table if not exists trade_memory (
       id text primary key, pair text not null, side text not null,
       cycle_start timestamptz not null,
       confidence int, score int, max_score int,
       adx_h1 double precision, adx_h4 double precision,
       persistence double precision, multi_tf boolean,
       result_5h text, move_pct_5h double precision,
       features vector(9) not null
   );
   create index if not exists trade_memory_features_ivf
     on trade_memory using ivfflat (features vector_cosine_ops)
     with (lists = 100);
   ```
5. (Optional, faster KNN) deploy the `match_trade_memory` RPC printed
   by `scripts/memory_query.py` — Python falls back to a SELECT + sort
   if the RPC is missing, so this is purely a performance step.

The `supabase` Python client is **not** in `pyproject.toml` — the
workflows install it ad-hoc with `pip install supabase>=2.0.0` and the
scripts import it lazily so existing local installs that don't need
the memory keep working without it.

## 11. Useful one-liners

```bash
# Latest cycle WR
python -c "import json,pathlib; d=json.loads(pathlib.Path('state/forecasts.json').read_text()); print(d.get('rolling_wr_5h'))"

# Force a manual cycle locally
python scripts/cycle_5h.py --once

# Run heuristic AI review without any token
python scripts/ai_review.py

# Run AI patcher (requires GITHUB_TOKEN env var)
GITHUB_TOKEN=$(gh auth token) python scripts/ai_patcher.py

# Index winning-setup memory into Supabase pgvector
SUPABASE_URL=https://<id>.supabase.co SUPABASE_KEY=ey... \
  python scripts/memory_index.py

# Refresh historical-analogs report
SUPABASE_URL=https://<id>.supabase.co SUPABASE_KEY=ey... \
  python scripts/memory_query.py
```

## 12. Permanent Fly.io deployment (free tier, auto-deploy on push)

The dashboard is hosted permanently on Fly.io's free tier so it stays
online 24/7 without depending on any active Devin session.  The public
URL is **`https://forex-wws2277.fly.dev/`** (Telegram Mini App entry:
`https://forex-wws2277.fly.dev/tg`).

Three files at the repo root drive the deploy:

- `fly.toml` — app `forex-wws2277`, region `fra`, internal port 8080,
  one always-on `shared-cpu-1x` 256 MB VM (`auto_stop_machines=false`,
  `min_machines_running=1`), HTTP healthcheck on `/api/cycle`.  No
  persistent volume — `state/` is regenerated by the 5h cycle workflow
  committing back to the repo, so the deploy stays inside the free tier.
- `Dockerfile` — `python:3.11-slim` with the `fastapi` / `uvicorn` /
  `yfinance` / `pandas` / `numpy` / `slowapi` deps, runs as a non-root
  user, exposes 8080, ships with a `curl /api/cycle` healthcheck.  Build
  context shrunk by `.dockerignore` (excludes `state/`, `reports/`,
  `.git`, `.venv`, `__pycache__`, `.agents`, `.github`, `scripts/`,
  `tests/`, `tradingview/`).
- `.github/workflows/deploy_fly.yml` — runs on every `push` to `main`
  that touches `app/**`, `static/**`, `fly.toml`, `Dockerfile`,
  `.dockerignore` or `pyproject.toml` (plus `workflow_dispatch`).
  Step 1 checks the `FLY_API_TOKEN` secret and **exits silently with a
  green status + `notice` annotation** when it's missing — fork-safe.
  Otherwise: `superfly/flyctl-actions/setup-flyctl@master` then
  `flyctl deploy --remote-only`.  Concurrency-limited per ref so
  rapid-fire pushes don't queue duplicate deploys.

### One-time setup (about 90 seconds)

1. Create a Fly.io account (free, no credit card for this size) at
   <https://fly.io/app/sign-up>.
2. `flyctl apps create forex-wws2277` (run once locally — pins the
   `forex-wws2277.fly.dev` subdomain).  This is the only command that
   has to leave your laptop; the workflow handles every subsequent
   deploy.
3. Generate a deploy token: <https://fly.io/user/personal_access_tokens>
   → "Create access token" → name it `github-actions`.
4. Add it as a repo secret: GitHub → Settings → Secrets and variables →
   Actions → "New repository secret" → name `FLY_API_TOKEN`, value =
   the token from step 3.
5. Push to `main` (or use **Actions → Deploy to Fly.io → Run workflow**)
   — the workflow runs `flyctl deploy --remote-only` and the dashboard
   appears at `https://forex-wws2277.fly.dev/` within ~2 minutes.

After that every push to `main` that changes app code or deploy infra
auto-redeploys with zero-downtime rolling restart.  No machine ever
sleeps (`auto_stop_machines=false`), so the dashboard stays online for
the existing `health_check` workflow and for the Telegram Mini App.

### Smoke-testing the Dockerfile locally

```bash
docker build -t forex-test -f Dockerfile .
docker run --rm -p 8080:8080 forex-test
curl -fsS http://127.0.0.1:8080/api/cycle | head -c 120
```

### Constraints / forbidden edits

- Don't add a persistent volume — `state/` is committed by the cycle
  workflow, mounting a Fly volume costs money and breaks the free tier.
- Don't change the VM size above `shared-cpu-1x` 256 MB — that's the
  free-tier ceiling.
- Don't set `auto_stop_machines=true` — the dashboard is polled every
  10 minutes by `health_check.yml` and must answer instantly.
- Don't bake any secrets into the image; everything ships via Fly env
  / GitHub Action secrets.
