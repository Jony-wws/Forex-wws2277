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
- **Real WR gate**: paper-trader opens a trade ONLY when both
  `forecast.probability_pct ≥ 70` AND `backtest_30d[pair].win_rate_pct ≥ 70`
  with `trades ≥ 5`. This was added because the user explicitly asked for
  "real 70% WR, not theoretical".

## Quick start (after fresh clone)

```bash
cd ~/repos/Forex-wws2277       # or wherever the repo is checked out
pip install -q -r teamagent/requirements.txt
bash scripts/start_all.sh      # spawns: orchestrator (→ scanner + paper_trader
                               # + state_committer + backtester + 60 agents)
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
| `orchestrator.py` | spawns ALL child processes (scanner / paper / backtester / state_committer / 60 agents) |
| `watchdog.py` | heartbeat-level health check, kills stale agents, MUST `continue` (not `pass`) on heartbeat_watchdog/orchestrator |
| `state_committer.py` | every 15 min: `git add+commit+push` of state/*.json so trade history survives across sessions |
| `dashboard/server.py` | FastAPI: `/api/forecasts`, `/api/forecast/{pair}`, `/api/open-trades`, `/api/closed-trades`, `/api/stats`, `/api/volume-profile/{pair}`, `/api/health`, `/api/agents`, `/api/backtest` |
| `dashboard/static/` | vanilla JS frontend, auto-refresh every 30 sec |
| `agents/` | 60 subprocess agents: 28 specialists + 14 analyzers + 10 learners + 5 health + 3 LLM |

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
7. **Real 70% WR gate**: paper_trader requires backtest WR ≥ 70% with ≥ 5
   trades on the pair before opening. Don't bypass this without explicit user
   request.

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
  forecast_scanner + paper_trader + backtester (without the 60 subprocess
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
  `https://59b8755c28a6-tunnel-ypdtvz8d.devinapps.com/`
  user / 750a8301e1ac3b0f174f666a0800b3f8
  (auto-login URL: `https://user:750a8301e1ac3b0f174f666a0800b3f8@59b8755c28a6-tunnel-ypdtvz8d.devinapps.com/`)
  NOTE: this URL changes every Devin session. The current value is updated by
  the agent at the start of each "продолжай"/"continue" session and committed
  to this file so the user always has the latest.
- Permanent dashboard (fly.io if deployed): see `infra/fly/` and the latest
  commit body for the URL. Recommended for 24/7 uptime without burning ACU.
- PR #1: `https://github.com/Jony-wws/Forex-wws2277/pull/1`
- All commits + state history:
  `https://github.com/Jony-wws/Forex-wws2277/commits/devin/1777586006-teamagent-rebuild`

## Honest known limitations (do NOT hide these)

- Real 30-day backtest of the EDGE-44 multi-TF technical scanner gives
  ~51-58% WR per pair. NO single pair currently achieves 70% WR. Therefore the
  paper_trader gate at 70% means new trades won't open until either
  (a) `strategy_search.py` finds a config with ≥70% WR,
  (b) the gate threshold is lowered, or
  (c) the strategy gains an edge from non-technical signals (LLM news, etc.).
- The first session (before commit discipline) lost 5000+ lines. Always commit.
- Yahoo Finance has occasional rate limits. The data layer caches per TTL.
