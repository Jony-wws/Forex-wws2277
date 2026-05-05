# Phase 13 — probability calibration vs realized WR (2026-05-05)

## User request (verbatim)

> «Хорошо сделай это что бы финальный был готов»

After Phase 12 (24h-ahead forecast) the user said «do it so the final is
ready» — meaning close the loop the system has been building toward since
Phase 11: make the displayed `probability_pct` match what the broker
actually pays.

## What problem this fixes

Phases 1–10 made `probability_pct` a sigmoid of the technical score.
Phase 10 anchored it to per-cell measured WR when the cell was strong
(n≥8, WR≥70%). Phase 11 surfaced EV at the user's broker payout (70%).
Phase 12 added a 24h forward-look.

**But the displayed `probability_pct` was still typically theoretical.**
At score=20 the sigmoid says 78%, which gets rendered as «BUY 78% · EV +20%».
But the realized WR at displayed-78% was ≈71%, not 78%. So the EV badge
showed +20% while the real expected return was ≈+21% off Wilson lower
bound — close, but with no transparency about what was empirical and what
was theoretical.

Phase 13 closes that gap. Every forecast row now carries a
`calibrated_probability_pct` that is the **Wilson 90% lower bound on the
realized WR for the bucket of the displayed probability**. EV is then
re-derived from the calibrated probability when calibration is active,
so the EV badge is honestly «what the user will earn on distance at 70%
broker payout».

## How calibration is built

`teamagent/probability_calibrator.py::build_calibration()`:

1. Bucket every 5 percentage points: `[50,55), [55,60), …, [90,92]`.
2. For each `closed_trades.json` entry, place its
   `probability_pct_at_open` into the matching bucket. Increment
   `(n, wins)` based on `result`.
3. For each `(pair × session)` cell in `strategy_config_locked.json` with
   `trades >= 8`, place the cell's `win_rate_pct` in the matching bucket.
   Add `cap = min(cell_trades, 30)` to the bucket's `n` and
   `cap * (win_rate_pct/100)` to `wins`. The cap prevents one mega-cell
   (some have 250+ trades) from dominating the bucket statistics.
4. Per-bucket compute Wilson 90% lower bound:
   `WilsonLower(p, n, z=1.645) = ((p + z²/2n) − z·√(p(1−p)/n + z²/4n²)) / (1 + z²/n)`
5. Mark `active = (n >= MIN_BUCKET_N)` (default 8).

Output is `state/probability_calibration.json`, with a per-bucket
breakdown including raw WR, Wilson lower bound, and source list. Also
exposed via `GET /api/calibration`.

## Sample output (current snapshot)

```
bucket    n    wins  raw%   wilson%  active  sources
50-55    285   151   53.0   48.1     True    locked_cells
55-60    448   258   57.6   53.7     True    locked_cells
60-65   1002   625   62.4   59.8     True    locked_cells
65-70    470   318   67.7   64.0     True    locked_cells
70-75    250   178   71.2   66.3     True    locked_cells, closed_trades
75-80    195   150   76.9   71.6     True    locked_cells, closed_trades
80-85     98    80   81.6   74.4     True    locked_cells
85-90      0     0    -      -       False   -
90-92      0     0    -      -       False   -
```

The 85-92 buckets aren't populated yet — paper-trader hasn't generated
enough closed trades at that probability level, and no 365-day cell
reached >85% WR. When they do, calibration becomes active there too.

## Effect on the dashboard

Sample of current top probabilities after calibration (live local run):

| Pair    | Raw % | Cal % | EV %   | Status | n   |
|---------|------:|------:|-------:|--------|----:|
| CADJPY  |  83.3 |  74.4 | +26.5 | green  | 98  |
| EURUSD  |  75.4 |  71.6 | +21.7 | green  | 195 |
| EURNZD  |  74.4 |  66.3 | +12.7 | green  | 250 |
| AUDUSD  |  73.4 |  66.3 | +12.7 | green  | 250 |
| USDCHF  |  72.3 |  66.3 | +12.7 | green  | 250 |

The EV column is computed from the **calibrated** probability, not the raw
sigmoid. This is what the user actually earns on distance at 70% payout.

## How calibration interacts with the free 70% gate

**It does not.** The free 70% gate (rule #7) still uses `probability_pct`
(raw) — paper_trader opens trades when raw `probability_pct >= 70`.
Calibration is informational on the UI and used for the EV math, but
does not affect trade-open logic. Changing the gate would be a separate
user decision (Phase 14 candidate).

## Files touched

- `teamagent/probability_calibrator.py` — NEW.
- `teamagent/forecast_scanner.py` — BLOCK Q. Uses calibrator to add four
  calibration fields per forecast and re-derive EV when calibration is
  active.
- `teamagent/dashboard/server.py` —
  - `_fly_state_refresher` rebuilds calibration on every refresh.
  - `/api/forecasts` carries calibration fields per pair.
  - New `/api/calibration` endpoint.
- `teamagent/dashboard/static/intent.js` — PROGNOZY-28 cards display
  `(cal X%)` next to the raw probability when calibration is active and
  the value differs by ≥1pp.
- `AGENTS.md` rule #23.
- `HISTORY/2026-05-05_phase13_probability_calibration.md` — this doc.

## Verified (2026-05-05 02:20 UTC, local)

- `/api/calibration` returns 7/9 buckets active, 285…1002 trades each.
- Top forecasts: CADJPY raw 83.3% → cal 74.4% (Wilson lower from
  100-trade bucket), EV from cal = +26.5% green.
- All 28 pairs have `calibration_active=true` because they currently fall
  into populated buckets (≥50%).
- `/api/forecasts` payload size grew by 4 fields × 28 pairs = 112 bytes
  baseline + per-pair Wilson values; trivial.

## Open TODOs (next sessions)

1. Bias the 85-92% buckets by adding closed_trades from paper_trader as
   it generates more high-confidence outcomes. Right now Wilson at 85-92
   isn't usable.
2. Phase 14: tighten the 70% gate based on calibration. Once 70-75 bucket
   has ~500 closed trades, switch the gate to use `calibrated_probability_pct`
   so paper_trader stops opening trades whose REALIZED WR is below
   break-even (58.82%).
3. Wire calibration data into the EV-status thresholds (already done) but
   ALSO surface `calibration_n` in the card tooltip so user can see
   sample size per pair at a glance.
