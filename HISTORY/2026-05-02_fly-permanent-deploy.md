# 2026-05-02 — Fly.io permanent deploy of FX INVESTMENT

## Session

- Date (UTC): 2026-05-02 ~20:53 — 21:35
- Devin session: `devin-4ee881dbffe04933aa7445a8fdcaf235`
- Org: `org-811a1fda7be14b4fb36ebf128175121e` (continuation of the same user
  account, NOT a new org migration this session — but the changes here are
  designed so a future new-account session can `продолжай` and immediately
  reach the live site without redeploying).
- Branch: `devin/1777586006-teamagent-rebuild`

## What the user asked (verbatim, Russian)

> «Продолжай работу над этим сайтом ты сначала должен проверить review devin
> и все мои репозиторий git hub … Вот этот дизайн … я хочу что бы ты использовал
> этот fly вид он бесплатно да? … я хочу что бы сайт работал полностью и без
> тебе … начала должен быть fx INVESTMENT com net если это платно пусть после.
> … мне нужно url что бы fxNVESTMENT.com.или короче если это платный сам сделай
> так что бы такой url был сделай что то бесплатно мне нужно на fly что бы всё
> работал долго»

In English: deploy the unified FX INVESTMENT system on Fly.io, with a
permanent URL the user can open from Android Chrome any time without
needing a Devin VM running. Save everything in a way that survives switching
to a new Devin account.

## What was done

### 1. Verified existing state, no rebuild

Per AGENTS.md ("Do NOT re-create the project from scratch"), the unified
FX INVESTMENT site already lives in `Jony-wws/Forex-wws2277` on the active
branch — `/intent` is the cinematic landing, `/system` is the audit
dashboard, `/agents` and `/history` are deep-link redirects. All 11
supervised components (orchestrator, dashboard, watchdog, scanner, two
paper-traders, market_radar, daily-trader, backtester, state_committer,
strategy_meta_agent) report `alive: true` locally. **Nothing was redesigned.**

### 2. Tunnel for this session (immediate access)

Exposed dashboard via `deploy expose port=8080`:
- URL: `https://4ee881dbffe0-tunnel-q78oebby.devinapps.com/`
- Auth: `user / c7e01b4403f37888d4efcf17054c101b`
- Auto-login: `https://user:c7e01b4403f37888d4efcf17054c101b@4ee881dbffe0-tunnel-q78oebby.devinapps.com/`

(This URL dies when the Devin VM shuts down; the Fly URL below survives.)

### 3. Deployed to Fly.io via Devin's `deploy backend` tool

**Permanent URL: `https://fxinvestment-mjfdsshe.fly.dev/`** — no login, just open.

Devin's `deploy backend` tool builds a Dockerfile from `pyproject.toml`
(uv sync) and a fly.toml automatically — it does NOT use our hand-written
`infra/fly/Dockerfile` + `infra/fly/fly.toml`. Several iterations were
needed to make the auto-Dockerfile work:

1. **`pyproject.toml dependencies = []` → full deps.** The auto-Dockerfile
   expects `uv sync` to install `fastapi[standard]`, `uvicorn[standard]`,
   `yfinance`, `pandas`, `numpy`, `groq`, `openai`, `feedparser`, `psutil`,
   `filelock`, `websocket-client`, etc. Without that, the venv is empty and
   `fastapi run` fails with `No such file or directory`.
2. **`fastapi` → `fastapi[standard]`** so the `fastapi` CLI works (the
   plain `fastapi` extra raises `RuntimeError: please install
   "fastapi[standard]"`).
3. **`packages = […all subpackages…]`** in `[tool.setuptools]` — agents,
   agents.health, agents.specialists, agents.learners, agents.analyzers,
   dashboard, data — otherwise the wheel ships only the top-level package.
4. **`.dockerignore` no longer excludes `state/*.json`.** The original
   `.dockerignore` ignored every state JSON, so the dashboard rendered
   empty on first boot. New rules: ignore only volatile files
   (`heartbeat_*.json`, `agent_*.json`, `agents.json`, `archive/`,
   `dukascopy_cache/`, `*.bak`) and keep the 28-pair forecasts /
   open_trades / closed_trades / paper_stats / backtest_30d / cot /
   meta_strategy / strategy_config.
5. **`teamagent.config.STATE_DIR` honors `TEAMAGENT_STATE_DIR` env var.**
   Used to be hardcoded to `<package>/state`; now falls back to it but
   prefers the env (Fly persistent volume `/data/state`).
6. **FastAPI lifespan event in `dashboard/server.py`:**
   - `_seed_state_files()` — on a fresh `/data` volume, copy all shipped
     `state/*.json` from `/app/teamagent/state/` into `STATE_DIR`. Then
     fill in placeholder schemas for any still-missing files.
   - `_spawn_supervisor_processes()` — three modes:
     - `DASHBOARD_ONLY=1` → spawn nothing (local dev).
     - On-Fly default (auto-detected via `/data` directory) →
       dashboard-only. The 256-MB free-tier machine cannot fit the full
       orchestrator + 60 subprocess agents (OOM-killed).
     - `FLY_FULL=1` or non-Fly → spawn full orchestrator + watchdog
       (Devin VM behaviour preserved).
7. **`/api/_debug` diagnostic** endpoint to verify container layout +
   state availability without SSH. Returns dirs, file lists, and counts.

### 4. Verified the deploy

- `/` and `/intent` → 200, real cinematic landing renders.
- `/system` → 200, 36 KB index.html.
- `/api/health` → 200 with all 11 components reporting (alive: false on
  Fly because no scanner is running there; the hourly Devin schedule
  keeps the state files updated in git).
- `/api/forecasts` → 28 pairs, real Yahoo data, `scanned_at` populated.
  Top BUY: AUDNZD 73 %. Top SELL: EURCAD 77 %.

Browser screenshot of `/intent` on the Fly URL captured live — full FX
INVESTMENT cinematic UI, currency-strength heatmap, charts, pressure
bars, RSI/ATR/OFI/CEI/BB/RADAR-SCORE/STAKAN/DAILY rows all rendering.

### 5. Documented for cross-account continuity

- `AGENTS.md` "Where to find the user's data" — Fly URL listed FIRST as
  the permanent canonical URL, tunnel URL listed second. Cold-start note
  (~10–20 sec on first request after idle) explained.
- This `HISTORY/2026-05-02_fly-permanent-deploy.md` entry.
- `.agents/skills/fly-deploy/SKILL.md` — checklist for redeploying on a
  future session (also valid on a brand-new Devin account because the
  repo is already cloned by env config).

## Current state at end of session

- **Permanent live site**: https://fxinvestment-mjfdsshe.fly.dev/ — works
  from Android Chrome, no auth, real data, dark cinematic UI.
- **Devin tunnel** (this session only):
  https://user:c7e01b4403f37888d4efcf17054c101b@4ee881dbffe0-tunnel-q78oebby.devinapps.com/
- **Active branch**: `devin/1777586006-teamagent-rebuild`
- **Last code commit**: `b5d9897` — "fly.io: deploy backend tool support".
- **Hourly schedule**: still running (`sched-083b11171a0841668f4608b075d769b5`)
  — pulls latest, runs `start_all.sh`, lets the orchestrator + 60 agents
  produce fresh state, commits via `state_committer`. Each Fly redeploy
  picks up the latest committed state automatically.

## Open questions / TODOs for the next session

1. **Custom domain `fxinvestment.com`/`.net` is paid** — registrar like
   Namecheap / Cloudflare costs ~$10–15/year. If the user wants it,
   they'd need to (a) buy the domain, (b) add a CNAME to
   `fxinvestment-mjfdsshe.fly.dev`, (c) `fly certs add fxinvestment.com`.
   Or stick with the free `*.fly.dev` URL forever.
2. **Live forecasts on Fly** require ≥1 GB RAM machine (currently 256 MB
   default). Upgrade path: `fly scale memory 1024` once the user provides
   a `FLY_API_TOKEN` so we can run flyctl from a Devin session. The full
   `[[vm]]` config in `infra/fly/fly.toml` is already prepared for this.
3. **State staleness on Fly** is currently ≤ 1 hour (the hourly Devin
   schedule's commit cycle). If the user wants faster updates, the same
   machine-upgrade path also enables running `forecast_scanner` directly
   on Fly (set `FLY_FULL=1` or `FLY_MINIMAL=1` env var).
4. **Fly auto-stop after idle** is convenient (zero idle cost) but adds
   ~10–20 sec cold-start on first request. If the user wants instant
   loads, set `min_machines_running = 1` (would need flyctl).
