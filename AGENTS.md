# AGENTS.md — instructions for any AI agent (Devin / Codex / Cursor / etc.)

This file is read automatically by AI assistants when they work in this repo.
Read it BEFORE doing anything else. It explains the project, conventions, and
how to continue work without re-asking the user.

## How to start a NEW session (зачем оно)

If the user just writes "продолжай" / "continue" — DO ALL OF THIS:

1. Read the latest commits: `git log --oneline -20`.
2. Install deps if needed: `pip install -q -r teamagent/requirements.txt`.
3. Start the system: `bash scripts/start_all.sh` (it auto-installs deps too).
4. Wait ~5 sec, then verify: `curl -s http://127.0.0.1:8080/api/health`.
5. Expose externally with the Devin `deploy expose port=8080` tool — you'll
   get a URL like `https://<ID>-tunnel-<TOKEN>.devinapps.com/` with HTTP Basic
   Auth (`user` / `<token>`).
6. Update the "Where to find the user's data" section below with the NEW URL
   + login + password, commit and push to this branch
   (`devin/1777586006-teamagent-rebuild`) so it lands in PR #1. This is how
   the next session in any account will know the live URL without asking.
7. Send the URL + login + password to the user as the FIRST message after
   start. The user is on Android Chrome — use the auto-login URL form
   `https://user:<password>@<host>/` so they can just tap and open it.

**Do NOT** re-create the project from scratch. Everything is already built.

## Project: FOREX AI 2026 — TeamAgent

Multi-agent paper-trading system for **28 forex pairs**.

- **Real data only**: Yahoo Finance (live + history), Dukascopy (1m cache),
  ForexFactory RSS (news blackout). NO simulators. NO synthetic / random data.
  NEVER add a simulator — the user will reject the PR.
- **Single source of truth**: PROGNOZY-28 table in the dashboard.
  Do NOT introduce a second meta-voting endpoint.
  `agents_for` / `agents_against` are integrated INSIDE each forecast row.
- **Probability is capped**: 50% min, 92% max. NEVER show 100%.
- **Free 70% gate (current, since 2026-05-01)**: paper-trader opens a trade as soon
  as `forecast.probability_pct ≥ 70` — independent of session and independent of
  per-(pair, session) backtest WR. The user explicitly requested this on 2026-05-01:
  *"минимум 70% есть он должен открыться не важно сколько там есть … не нужно 70%
  на каждом валюте на каждом сессии"*. The per-session strategy_search results are
  STILL computed hourly and STILL used to enrich the chosen variant (side flip via
  contrarian/fade-RSI rules + fixed_expiry_h) when a qualified variant exists, but
  they no longer block the trade. The earlier strict gate (`backtest WR ≥ 70` AND
  per-session WR ≥ 70 with ≥ 10 trades) is preserved in git history if you ever
  need to revert.

## Quick start (after fresh clone)

```bash
cd ~/repos/Forex-wws2277       # or wherever the repo is checked out
pip install -q -r teamagent/requirements.txt
bash scripts/start_all.sh      # spawns: orchestrator (→ scanner + paper_trader
                               # + state_committer + backtester + 64 agents)
                               # + watchdog + dashboard on :8080
```

Dashboard: `http://127.0.0.1:8080/`. To expose externally use
`deploy expose port=8080` from a Devin tool — Fly.io deploy scripts live in
`infra/fly/` (see "Deployment" below).

Stop everything: `bash scripts/stop_all.sh`.

## Layout (everything under `teamagent/`)

| File | Role |
|---|---|
| `config.py` | 28 PAIRS, sessions, MIN_PROBABILITY=0.70, MAX_PROBABILITY=0.92, intervals, NEWS_BLACKOUT_PENALTY=5 |
| `data/yahoo.py` | `fetch(pair, interval, period)` + `latest_price()` + `settlement_price()` with TTL cache |
| `data/dukascopy.py` | `get_30d_1m(pair)` — yfinance fallback for the bi5 archive |
| `data/news.py` | `is_blackout(pair, when, ±30min)` — ForexFactory RSS, high-impact only |
| `indicators.py` | RSI, EMA, ATR, Bollinger %B, Momentum, CEI, OFI, VWAP, BBP, `all_indicators()` |
| `volume_profile.py` | POC/VAH/VAL + big_players (≥80th pctl) + `forecast_to_utc5_midnight` (no_return_levels) |
| `forecast_scanner.py` | `evaluate_pair(pair)` returns one unified forecast (the "PROGNOZY-28" source) |
| `paper_trader.py` | binary $50/85% trades, 1-4h expiry, settles on real Yahoo close. Gated by backtest WR. |
| `backtester.py` | hourly 30-day walk-forward backtest per pair, writes `state/backtest_30d.json` |
| `strategy_search.py` | (run on demand) tries 30+ scoring/expiry/session variants to find ≥70% WR config |
| `orchestrator.py` | spawns ALL child processes (scanner / paper / backtester / state_committer / 64 agents) |
| `watchdog.py` | heartbeat-level health check, kills stale agents, MUST `continue` (not `pass`) on heartbeat_watchdog/orchestrator |
| `state_committer.py` | every 15 min: `git add+commit+push` of state/*.json so trade history survives across sessions |
| `dashboard/server.py` | FastAPI: `/api/forecasts`, `/api/forecast/{pair}`, `/api/open-trades`, `/api/closed-trades`, `/api/stats`, `/api/volume-profile/{pair}`, `/api/health`, `/api/agents`, `/api/backtest` |
| `dashboard/static/` | vanilla JS frontend, auto-refresh every 30 sec |
| `agents/` | 64 subprocess agents: 28 specialists + 16 analyzers (incl. fundamental_macro from FRED + cot_positioning from CFTC, added 2026-05-01) + 12 learners (incl. WR floor monitor + weekly loss review) + 5 health + 3 LLM |
| `fundamentals.py` | FRED public CSV fetcher: policy rate / 10y bond yield / CPI YoY for USD/EUR/GBP/JPY/CHF/AUD/CAD/NZD; per-pair macro tilt; 24h cache (no API key) |
| `cot.py` | CFTC public Socrata API: weekly speculator long/short for EUR/GBP/JPY/CHF/AUD/CAD/NZD futures; per-pair contrarian z-score signal; 24h cache (no API key) |

## State files (in `teamagent/state/`)

| File | What | Persisted in git? |
|---|---|---|
| `forecasts.json` | current 28-pair forecasts (every 5 min) | YES — auto-committed by state_committer |
| `open_trades.json` | active trades | YES |
| `closed_trades.json` | history with WIN/LOSS | YES |
| `paper_stats.json` | total/wins/losses/WR/PnL | YES |
| `backtest_30d.json` | hourly per-pair real-data backtest | YES |
| `strategy_config.json` | selected config per pair+session (output of strategy_search) | YES |
| `agents.json` | orchestrator's view of 60 processes | NO (volatile) |
| `heartbeat_*.json` | per-agent pulse | NO (volatile) |
| `recommended_restart.json` | watchdog's diagnostic dump | NO |

## Conventions (do NOT break)

1. **Commit EVERY change after each phase**. The first session lost 5000+ lines
   because nothing was committed. Make a commit after each meaningful edit.
2. **NEVER use simulators / random / fake data**. Real Yahoo + Dukascopy +
   ForexFactory only. The user will revert the PR if you add a fake source.
3. **Single source of truth**: PROGNOZY-28 table = paper_trader's source.
   NEVER add a separate meta-voting endpoint or table.
4. **Probability cap**: always 50–92%. Never expose 100%.
5. **News blackout penalty** REDUCES `abs(score)` toward zero (both BUY and
   SELL); don't reintroduce the unconditional `-5` bug we already fixed.
6. **Watchdog `_scan()` MUST `continue`** on `heartbeat_watchdog.json` and
   `heartbeat_orchestrator.json` — never `pass` (that bug killed the whole
   orchestrator).
7. **Free 70% gate (since 2026-05-01)**: paper_trader opens trades when
   `forecast.probability_pct ≥ 70` — period. Per-session backtest WR is NOT a
   blocker; it's used only to enrich the chosen variant (side flip / fixed expiry).
   This is the user's explicit override of the earlier strict gate. Do NOT
   reintroduce the strict gate without an equally explicit user request.
8. **strategy_search re-trains every 5 days** (since 2026-05-01, per user request
   to save ACU): `LOOP_INTERVAL_SEC = 5 * 24 * 3600`. Each sweep re-evaluates 120
   variants × 4 sessions × **365-day** Yahoo history per pair (≈50 min). Between
   sweeps the system trades on the most recent strategy_config.json. Use
   `python -m teamagent.strategy_search --relock` to manually re-lock the
   baseline if a new sweep beats the locked one.

9. **Locked baseline strategy**: after the first valid 365-day sweep,
   strategy_search auto-snapshots `strategy_config.json` →
   `strategy_config_locked.json`. paper_trader uses the locked file as a
   fallback whenever the live strategy_config is empty (e.g. sweep in progress
   on a fresh session). Locked file is committed by state_committer so it
   survives across sessions. Re-lock manually with `--relock` after a new
   confirmed-better sweep.

10. **WR floor monitor (alert, NOT a gate)**: `learner_wr_floor_monitor` agent
    computes rolling WR over last 50 closed trades every 5 min. If it drops
    below 70%, dashboard shows ⚠️ alert. Trade-open gate is unchanged (still
    free 70% on probability). Floor monitor is purely diagnostic — "time to
    trigger fresh sweep".

11. **Weekly loss review**: `learner_weekly_loss_review` agent runs every 6h,
    summarizes losses from the last 7 days by pair / session / hour UTC /
    direction, identifies pairs with WR ≤ 40% (≥3 trades). Surfaces the
    "blind spots" of the current strategy on the dashboard.

## Optional API keys (env vars)

The 3 LLM agents are no-op if these aren't set; the rest of the system still
works without them.

- `GROQ_API_KEY`
- `GOOGLE_API_KEY`
- `OPENROUTER_API_KEY`
- `DERIV_DEMO_TOKEN` — only needed if you want Deriv real quotes

## Deployment & permanent URL

The user works from Android Chrome and can NOT keep a Devin VM running 24/7.
For a permanent URL (Devin tunnel dies when the VM dies), use:

- **Fly.io** (recommended, free tier ok): `infra/fly/Dockerfile` and
  `infra/fly/fly.toml` are checked in. Deploy from a Devin session via the
  `deploy backend` tool. The fly app runs the FastAPI dashboard +
  forecast_scanner + paper_trader + backtester (without the 64 subprocess
  agents — those stay Devin-session-only because of resource limits).
- **Devin Schedule** (already configured, sched-083b11171a0841668f4608b075d769b5):
  hourly recurring session that runs `start_all.sh`, waits 10 min,
  commits state, exits. Survives because state_committer pushes to git.

## Cross-session continuity (no re-explanation needed)

The user uses Android Chrome. Their workflow:

1. New session in any org/account.
2. Repo `Jony-wws/Forex-wws2277` is auto-cloned by env config.
3. User writes "продолжай" — that's it.
4. The agent reads this AGENTS.md, runs `bash scripts/start_all.sh`, opens the
   dashboard, asks what to work on (or continues from latest commit message).

This file is the single source of context — whether or not the per-org
Knowledge Note is loaded.

## Devin Schedule (already running)

Schedule ID: `sched-083b11171a0841668f4608b075d769b5`
Frequency: `0 * * * *` (every hour)
Branch: `devin/1777586006-teamagent-rebuild`

What it does each hour: pulls latest, runs `start_all.sh`, waits ~10 min for
forecast_scanner / paper_trader / backtester / state_committer to do their
thing, then `stop_all.sh` and exits. State is auto-committed via
`state_committer`.

## Where to find the user's data

- Live dashboard (current Devin session, dies when session ends):
  `https://8b14ed6c3cae-tunnel-pchp0vd8.devinapps.com/`
  user / bfb871a7d9c5bc32830e1df7d8956536
  (auto-login URL: `https://user:bfb871a7d9c5bc32830e1df7d8956536@8b14ed6c3cae-tunnel-pchp0vd8.devinapps.com/`)
  NOTE: this URL changes every Devin session. The current value is updated by
  the agent at the start of each "продолжай"/"continue" session and committed
  to this file so the user always has the latest.
- Permanent dashboard (fly.io if deployed): see `infra/fly/` and the latest
  commit body for the URL. Recommended for 24/7 uptime without burning ACU.
- PR #1: `https://github.com/Jony-wws/Forex-wws2277/pull/1`
- All commits + state history:
  `https://github.com/Jony-wws/Forex-wws2277/commits/devin/1777586006-teamagent-rebuild`

## Honest known limitations (do NOT hide these)

**Latest state (May 2026 — 365-day sweep + STRICT gate + martingale):**
- 15 of 112 (pair, session) cells achieve ≥70% WR on real **365-day** Yahoo data.
- Per session: Asia 3/28, London 3/28, Overlap 5/28, NY 4/28.
- 7/28 pairs qualify globally.
- paper_trader STRICT_QUALIFIED_GATE=True: opens trades ONLY on qualified
  cells (per-session OR pair-global). No baseline-fallback. This guarantees
  real ≥70% WR (not theoretical).
- STAKE_USD = $1 (was $50). Martingale 1→2→4→reset enabled.
- Estimated 1-3 trades/day average (sometimes 0, sometimes 5+).

**Why 90d (not 60d, not 180d):**
- 60d sweep: 36/112 cells but most London cells were over-fit to recent regime.
- 180d sweep: 25/112 cells (under-trained for some seasonal patterns).
- 90d sweep: 37/112 cells with healthier distribution (Asia 5 vs 1, NY 4 vs 2).
- 120 variants (was 60→90→120). New v118_contra_emph_meanrev + v77_overlap_emph_momentum
  + v43_full_mtf_momentum each win 3-4 cells.

**Why ALL 112 cells ≥70% WR is hard / probably impossible without new data:**
- Asia and NY sessions are intrinsically tougher for technical-only signals
  (more efficient pricing, lower volume).
- EURUSD, USDCHF, CHFJPY and similar majors have ~64-67% structural ceiling
  on this scanner regardless of variant.
- Pushing all cells to ≥70% would require non-technical signals: LLM
  news/sentiment (need API keys), COT data, order flow.
- **Do NOT lower the 70% gate to fake compliance** — the system is honest
  about which cells qualify and which don't. The user explicitly required
  "real 70% WR, not theoretical".

**Operational:**
- The first session (before commit discipline) lost 5000+ lines. Always commit.
- Yahoo Finance has occasional rate limits. The data layer caches per TTL.

## Cross-account / cross-chat continuity (CRITICAL)

The user works across multiple Devin accounts/orgs/chats and explicitly
requires the system to "just work" with `продолжай` on a fresh account/chat.

To enforce this we maintain **multiple layers of redundancy**:

1. **This AGENTS.md** — primary context file. Read first.
2. **`SESSION_STATE.md` in this repo** — full snapshot of strategy state,
   known limitations, command checklist.
3. **`SESSION_STATE.md` mirrored in 5 other repos** of the user (`FOREX`,
   `FOREX21`, `Forex-wws2`, `Forex-wws22`, `Forex-wws27`) — points back to
   this canonical repo. So even if user opens any of those repos, the agent
   knows to come here.
4. **Devin Knowledge Note** (when applicable) — copy of SESSION_STATE.md at
   org-level so it auto-injects into context.
5. **PR #1 description and commit messages** — narrate every step.
6. **`state/*.json` auto-committed every 15 min** — full trade history,
   strategy config, paper stats survive in git, not just in VM memory.

When the user says "продолжай" / "continue" on a NEW account/chat:
- read this AGENTS.md and `SESSION_STATE.md`
- run `bash scripts/start_all.sh`
- open dashboard externally (`deploy expose port=8080` Devin tool)
- update the URL/Basic-Auth lines in this file (commit immediately)
- send the user the new URL via `message_user`
- ask what to work on (or continue improving strategies if no other task)
