# Phase 13 deploy to Fly.io (2026-05-05, 02:50–03:00 UTC)

## User request (verbatim)

> «Провер всё репозиторий и review devin вот сообщение от прошлого devin
> Merge PR #24 в main
> После merge — следующая Devin сессия запустит deploy backend чтобы
> Phase 13 попал на live https://fxinvestment-bvrylahr.fly.dev/»
>
> «Ещё вид нужно создать новый сайт так что бы вес обноание работал а то
> на старом сайте старый код так ?»

## What was already done before this session

- PR #24 (`phase13: probability calibration vs realized WR (Wilson lower
  bound)`) was already merged into `main` on 2026-05-05 02:30 UTC by the
  previous Devin session. Merge commit `8013a14`. Branch:
  `devin/1777947571-phase13-prob-calibration` → `main`.
- The previous Devin did NOT run `deploy backend`, so the live site at
  `https://fxinvestment-bvrylahr.fly.dev/` was still running the pre-merge
  code (Phase 12). `/api/calibration` on `bvrylahr` returned a populated
  table because that fly app had a stale `probability_calibration.json`
  in its volume from earlier manual experiments — but the deployed image
  itself did not have BLOCK Q in `forecast_scanner.py`.

## What this session did

1. Cloned `Jony-wws/Forex-wws2277`, confirmed `main` HEAD = `8013a14`
   (Phase 13 merged).
2. Ran `pip install -q -r teamagent/requirements.txt`.
3. Ran `python -m teamagent.probability_calibrator` to generate
   `state/probability_calibration.json` with **7 active buckets**
   (50–55, 55–60, 60–65, 65–70, 70–75, 75–80, 80–85), 15 closed trades
   used, 112 locked-cell entries used. Buckets 85–90 and 90–92 stay
   inactive until paper-trader produces enough closed trades there.
4. Ran `python -m teamagent.forecast_scanner` for one pass — produces
   `state/forecasts.json` where BLOCK Q populates calibration fields on
   every pair. Result: **27/28 pairs with `calibration_active=True`**
   (only the bucket-50 outliers below 50% display-probability skip
   calibration, and EURJPY currently sits exactly at 50.x boundary).
5. Used Devin's `deploy backend --dir /home/ubuntu/repos/Forex-wws2277
   --volume true` to ship the image with fresh state files baked into
   `/app/teamagent/state/` (per fly-deploy SKILL — `deploy backend`
   uses pyproject.toml's `package-data` `teamagent/state/*.json`).

## New live URL

**`https://fxinvestment-sldppfwz.fly.dev/`** — Phase 13 active.

`bvrylahr` is the previous deploy and now stale; the canonical Fly
permanent URL changes per `deploy backend` call. Update the
`fly-deploy` SKILL's "Live URL (canonical)" section if a future session
deploys to a different subdomain.

## Verification (curl)

```
$ curl -s https://fxinvestment-sldppfwz.fly.dev/api/calibration | jq
{
  "as_of": "2026-05-05T02:53:34Z",
  "min_bucket_n": 8,
  "wilson_z": 1.645,
  "n_closed_trades_used": 15,
  "n_locked_cells_used": 112,
  "buckets": {
    "50": { "n": 285, "wr_raw_pct": 52.98, "wilson_lower_pct": 48.11, "active": true },
    "55": { "n": 448, "wr_raw_pct": 57.59, "wilson_lower_pct": 53.71, "active": true },
    "60": { "n": 1002, "wr_raw_pct": 62.38, "wilson_lower_pct": 59.83, "active": true },
    "65": { "n": 470, "wr_raw_pct": 67.66, "wilson_lower_pct": 64.02, "active": true },
    "70": { "n": 250, "wr_raw_pct": 71.20, "wilson_lower_pct": 66.28, "active": true },
    "75": { "n": 195, "wr_raw_pct": 76.92, "wilson_lower_pct": 71.61, "active": true },
    "80": { "n":  98, "wr_raw_pct": 81.63, "wilson_lower_pct": 74.38, "active": true },
    "85": { "n":   0, "active": false },
    "90": { "n":   0, "active": false }
  }
}
```

`/api/forecasts` carries the BLOCK Q fields per pair:

```
EURUSD prob 71.2 → cal 66.3 (n=250)
GBPUSD prob 66.7 → cal 64.0 (n=470)
USDJPY prob 59.2 → cal 53.7 (n=448)
USDCHF prob 72.3 → cal 66.3 (n=250)
USDCAD prob 73.2 → cal 66.3 (n=250)
… 27/28 pairs with calibration_active=True
```

The PROGNOZY-28 detail pill (`data-side` element in `intent.js`) now
renders `«SELL 71% (cal 66%) · EV +Y%»` when calibration is active and
the calibrated value rounds to a different integer than the raw
probability — exactly what the PR #24 description specified.

## Open items / known limits

- `_fly_state_refresher` is gated by `FLY_DASHBOARD_REFRESH=1` env var
  which is **off by default** on the slim fly.toml that
  `deploy backend` auto-generates. Calibration on the live site will
  therefore stay anchored to the state files baked into the image
  until the next `deploy backend` (or until the Devin hourly schedule
  runs `state_committer` and someone redeploys).
  Workaround: every time Phase 13 buckets need a refresh, run
  `python -m teamagent.probability_calibrator` +
  `python -m teamagent.forecast_scanner` locally and redeploy.
  Better fix (Phase 14 candidate): pass
  `FLY_DASHBOARD_REFRESH=1` via `fly secrets` after deploy, or vendor
  a fly.toml that the deploy tool will pick up.
- Buckets 85–90 and 90–92 still inactive (n=0). They will activate
  automatically once paper-trader closes trades at probability ≥85%.
- The fly app subdomain changed (`bvrylahr` → `sldppfwz`). Update
  any user-facing bookmarks.

## Files touched

- (no source changes — Phase 13 code already on `main` from PR #24)
- `teamagent/state/probability_calibration.json` — **new**, generated
- `teamagent/state/forecasts.json` — regenerated by one scan
- This `HISTORY/2026-05-05_phase13_deploy_to_fly.md` — documentation

State file changes are NOT committed to `main` directly (Devin policy
forbids direct push). They were baked into the Fly image via
`deploy backend` and will be re-committed by the hourly Devin Schedule
`sched-083b11171a0841668f4608b075d769b5` on its next tick.
