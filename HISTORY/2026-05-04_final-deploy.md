# 2026-05-04 final deploy / recovery

## User request (verbatim summary)

User asked to recover PR #1 branch `devin/1777586006-teamagent-rebuild`: restore the previous better `strategy_config.json`, fix Fly.io `FLY_FULL=1`, update dead static shim URL, deploy, expose dashboard, save context, and avoid rebuilding the system from scratch or adding fake/simulated data.

User later clarified they have no Fly token and want everything done for free without further user action, with a ready product link.

## What was done

1. Checked out `Jony-wws/Forex-wws2277` on `devin/1777586006-teamagent-rebuild`.
2. Read `AGENTS.md`, `SESSION_STATE.md`, setup files, and PR #1 review/comments through Devin git tools.
3. Restored `teamagent/state/strategy_config.json` from commit `f80fc53`.
   - Restored result: 30/112 qualified cells.
   - Per session: Asia 4, London 12, Overlap 5, NY 9.
   - 9 globally qualified pairs: USDCHF, USDCAD, NZDUSD, EURGBP, EURJPY, GBPJPY, GBPCAD, CADJPY, AUDNZD.
4. Verified Fly `FLY_FULL=1` logic in `teamagent/dashboard/server.py` and made env flag parsing explicit via `_flag_enabled()`.
5. Deployed to Fly with volume:
   - `https://fxinvestment-uqfprqce.fly.dev/`
6. Verified full mode on Fly starts `orchestrator` + `watchdog`, but the free/current Fly machine OOM-kills the app after about 90 seconds. Without a Fly token or manual scale, full 64-process 24/7 Fly is not stable.
7. Reverted Fly runtime to stable free dashboard-only mode and verified:
   - `/api/health` OK
   - `/api/forecasts` returns 28 pairs
8. Started the full system on Devin VM with `bash scripts/start_all.sh` and verified:
   - `/api/health` OK
   - orchestrator/watchdog/forecast_scanner/paper_trader/strategy_search/strategy_meta_agent alive
   - `/api/forecasts` returns 28 pairs, `scanned_at=2026-05-04T15:38:09.745091+00:00`
   - `/api/open-trades` count 14 during verification
9. Exposed live Devin VM dashboard:
   - Auto-login: `https://user:5f457c9656cd820841749ce6f3785c00@d2a19c266c48-tunnel-rbyxmhrg.devinapps.com/`
   - Host: `https://d2a19c266c48-tunnel-rbyxmhrg.devinapps.com/`
   - Basic Auth: `user` / `5f457c9656cd820841749ce6f3785c00`
10. Updated `teamagent/dashboard/static/static-shim.js` to point at current Fly backend.
11. Fixed `scripts/build_static_mirror.sh` to create `api/regime` and `api/analyst` directories before baking per-pair files.
12. Rebuilt and deployed static mirror:
    - `https://static-build-lqdncvmx.devinapps.com/`
    - Latest static deploy succeeded after removing zero-byte WARN placeholder JSON files.
13. Created `INSTRUCTIONS_FOR_OTHER_PLATFORMS.md`.
14. Updated `AGENTS.md`, `SESSION_STATE.md`, and Fly deploy skill URL/context.

## Current URLs

- Full live Devin tunnel: `https://user:5f457c9656cd820841749ce6f3785c00@d2a19c266c48-tunnel-rbyxmhrg.devinapps.com/`
- Fly.io stable dashboard: `https://fxinvestment-uqfprqce.fly.dev/`
- Static CDN mirror: `https://static-build-lqdncvmx.devinapps.com/`

## Current configuration

- `STRICT_QUALIFIED_GATE=False`
- `FORECAST_SCANNER_INTERVAL_SEC=120`
- `DASHBOARD_REFRESH_SEC=15`
- `ENSEMBLE_MIN_AGREEMENT_PCT=60`
- Repo `fly.toml` still records `FLY_FULL="1"`, but Devin deploy backend did not apply repo `fly.toml` env directly. A packaged/forced full-mode test confirmed full mode launches but OOMs on current free Fly resources.

## Known limitations / next work

- Full 64-process mode is available on the Devin VM tunnel, not stable on the current free Fly machine due to OOM.
- Static deploy now succeeds after deleting zero-byte placeholder JSON files during build.
- Need to continue improving strategy quality from 30/112 toward 60+/112 and eventually 80+/112 before re-enabling strict gate.
- Do not add simulators/fake data.
