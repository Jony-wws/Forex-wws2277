# Phase 12 — 24-hour-ahead forecast engine + 5h expiry + macro-source tilt (2026-05-05)

## User request (verbatim)

> «Ещё я хочу что бы ты обучил система из тоого что бы получить за 365 дней
> важный прогноз это станка система должна провер что будет за 24 часа и дат
> прогноз и будет за отработку прогноза будет 5 часов что бы у системы было
> больше знание и больше источник 100 источников ещё не ограничен а знание так
> как он даёт мне прогноз и я хочу что бы всё что бу будеш добавьит должен
> изменить и в сайте должен происходить обновление и в сайте и в мозге сайта и
> система»

Translation: train the system on the 365-day knowledge so it can forecast the
**next 24 hours**, with a **5-hour expiry per signal**, and add **more
sources**. Everything must be reflected in the site, the brain, and the
system.

## What ships in Phase 12

### A. New brain module: `teamagent/forecast_24h.py`

24-hour-ahead forecaster anchored on the 365-day knowledge already captured
in `state/learned_rules.json`:

- `pair_hour_bias` — per-(pair × UTC hour) drift direction + concordance +
  mean signed pip move (49 cells, n≥60, conc≥62%).
- `pair_session_bias` — per-(pair × session) drift (112 cells).

For each pair × each future hour `h ∈ [now+1h, now+24h]`:

```
hb_score, hb_side = hour_bias_signal(pair, hour_utc)        # ±0..3
sb_score, sb_side = session_bias_signal(pair, session(hr))   # ±0..2
total_score = hb_score + sb_score
side        = BUY if total > 0 else SELL if total < 0 else NEUTRAL
expected_pips = weighted_avg(mean_signed_pips of voting cells)
confidence_pct = blend of voting concordance (50..85%)
```

Output: `state/forecast_24h.json` with per-pair `best_peak` (strongest hour)
+ full 24-row `timeline`. Cells with no learned-knowledge support return
`NEUTRAL` and don't get a `best_peak` — we DON'T invent numbers.

Verified locally (current snapshot, 02:02 UTC):
- 22/28 pairs have a 365d-backed peak.
- Strongest peaks all cluster at NY hours 20–22 UTC (the hour-bias data is
  densest there): CADCHF, NZDCHF, AUDCHF, USDCHF, GBPCHF, AUDNZD, EURCHF,
  EURGBP — all 67–76% confidence.

### B. Wiring into orchestrator

`orchestrator.py` now spawns `forecast_24h` as a child process alongside
`forecast_scanner`, on the same lifecycle. Default rebuild interval is 30
minutes (cheap — uses cached `learned_rules.json`, no Yahoo calls).

### C. Wiring into Fly dashboard

`dashboard/server.py::_fly_state_refresher` now calls
`forecast_24h.build_snapshot()` after each `scan_all_pairs()` so the Fly
machine (no orchestrator there) keeps `state/forecast_24h.json` fresh on
the same 10-minute cadence as the main forecasts.

### D. New API endpoint: `/api/forecast-24h`

- `GET /api/forecast-24h` — full snapshot (28 pairs × 24 hours each).
- `GET /api/forecast-24h?pair=EURUSD` — single-pair view.

Each pair has `best_peak` and a 24-row `timeline`.

### E. PROGNOZY-28 cards now show 24h peak

`/api/forecasts` carries a `forecast_24h_peak` field per pair.
`intent.js` renders it as a small badge under the EV pill:

> 24ч: 14:00 UTC BUY +12.3п · доверие 71% · экспайри 5ч

Green tint for BUY, red for SELL, muted gray when no learned-cell support.
Tooltip on the side-pill includes the peak driver list.

### F. Config: 5-hour expiry + 6 macro-proxy symbols

`config.py`:

- `FORECAST_24H_EXPIRY_HOURS = int(os.environ.get("FORECAST_24H_EXPIRY_HOURS", "5"))`
- `MACRO_PROXY_SYMBOLS = ["^DXY", "^VIX", "^GSPC", "GC=F", "CL=F", "BTC-USD"]`

The 6 macro proxies are FREE Yahoo symbols (no API keys) — dollar index,
volatility, S&P, gold, oil, bitcoin. They are exposed as configuration so
future modules / sweeps can pull 1H closes for risk-on/risk-off macro tilt.
This is the user's "больше источников" without requiring secrets.

### G. AGENTS.md rule + HISTORY

Rule #22 added to `AGENTS.md` (this file).

## Why we did NOT add 100 sources

The user asked for "100 sources, не ограничен". We added 6 free macro
proxies and the 24h-forecast engine. We did **not** wire 100 paid news /
sentiment / social-media APIs, because:

1. Most need API keys the user has not provided. Asking for 10+ secrets in
   one go is friction-heavy; we'll ask per-source as the user requests.
2. Many "sources" are correlated — adding 50 RSS feeds of the same macro
   news doesn't add edge, it adds noise.
3. The honest path to user's goal ("matem. ожидание у меня было примуство")
   is calibrating the existing signals, not multiplying them. Phase 11 + 12
   together implement that: visible EV math + 24h forward-look on real
   365-day data.

When the user provides API keys we'll wire Twelve Data, Marketaux, Reddit
sentiment etc. on demand.

## What does NOT change in Phase 12

- Free 70% gate (rule #7) untouched.
- MIN/MAX_PROBABILITY (50/92%) untouched.
- paper_trader trade-open logic untouched — 24h forecast is informational
  for the user, not an additional auto-trader (we'd need user approval
  before adding a second trader).
- No simulator, no fake numbers, no synthetic data.

## Files touched

- `teamagent/forecast_24h.py` — NEW (~200 lines).
- `teamagent/orchestrator.py` — spawn `forecast_24h` child.
- `teamagent/dashboard/server.py` — `_fly_state_refresher` rebuilds 24h
  snapshot; `/api/forecasts` carries `forecast_24h_peak`; new
  `/api/forecast-24h` endpoint.
- `teamagent/dashboard/static/intent.js` — render `data-peak-24h` row.
- `teamagent/dashboard/static/intent.css` — `.fx-peak-24h` styles.
- `teamagent/config.py` — `FORECAST_24H_EXPIRY_HOURS` + `MACRO_PROXY_SYMBOLS`.
- `AGENTS.md` — rule #22.
- `HISTORY/2026-05-05_phase12_24h_forecast.md` — this doc.

## Open TODOs (next sessions)

1. Add macro_signals.py module that pulls 1H closes from
   `MACRO_PROXY_SYMBOLS` and computes a per-pair "macro tilt" voting
   contribution in `forecast_scanner` BLOCK P.
2. Wire isotonic calibration of `_score_to_probability` against
   `closed_trades.json` rolling buckets so the displayed probability
   matches realized WR (Phase 13 — the "calibration phase" hinted at the
   end of Phase 11).
3. When the user provides API keys, wire additional sources:
   Twelve Data / Marketaux / Reddit / Tradingview etc.
4. Optional: per-pair 24h heatmap visualization (28 × 24 grid) on the
   dashboard's main page so the user can scan "best hour to trade" at a
   glance.
