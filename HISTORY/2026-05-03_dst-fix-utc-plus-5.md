# 2026-05-03 — DST fix: market hours 1-hour offset bug + UTC+5 user-local timestamps

Branch: `devin/1777831858-single-source-and-health`
PR:     https://github.com/Jony-wws/Forex-wws2277/pull/8
Live:   https://fxinvestment-dhaftcbe.fly.dev/intent
Static: https://static-build-fukmtgwy.devinapps.com/

## What the user reported

> «Система не приавилно счает время пусть он будет работать только по utc 5+
>  рынка будет открыт через 11 минут но на сайте напсат через 1 час 11 минут
>  а на tradingwiew 11 минут ещё вопрос а все сделки будет ли открывается да ?
>  Система сам будет открыта сделку когда есть 70% win rate ? Да на всех
>  прогноза на всех режимах?»

Two issues:

1. Site countdown to forex open was off by **exactly 1 hour** vs TradingView.
2. User wanted local timestamps in **UTC+5** (his timezone).

Plus two trade-mechanics questions about the 70 % gate.

## Root cause of the 1-hour offset

`teamagent/market_hours.py` hard-coded **Sunday 22:00 UTC / Friday 22:00 UTC**
year-round. That's correct only when the US is on Eastern Standard Time
(EST = UTC-5, ~early November to mid-March). During US daylight-saving
time (EDT = UTC-4, mid-March to early November) the same NY-anchored 17:00
moment is **21:00 UTC**, not 22:00 UTC.

It is currently May 2026 → EDT → forex opens at 21:00 UTC, but the site
was using 22:00 UTC, hence the exact 1-hour offset.

## Fix

### `teamagent/market_hours.py`
- Anchor open/close to **NY-local** times via `zoneinfo.ZoneInfo("America/New_York")`.
- `is_market_open()`, `next_open()`, `next_close()` now compute the NY-local
  weekday/hour, then convert back to UTC. zoneinfo handles DST automatically:
  same code returns 21:00 UTC in summer, 22:00 UTC in winter.
- `market_status()` now also includes:
  - `as_of_utc_plus_5` — current time in user's locale (UTC+5)
  - `next_event_utc_plus_5` — next open/close in UTC+5
  - `next_event_ny` — same in NY-local for cross-reference

### `teamagent/final_signal.py`
- `_GlobalContext` market_detail now appends the UTC+5 timestamp:
  `«Рынок закрыт. Откроется через 4м 55с (2026-05-04 02:00 (UTC+5))»`.

### `teamagent/dashboard/static/app.js`
- `clientMarketStatus()` (the browser-side fallback used when the static
  snapshot is stale) was also DST-broken. Rewritten to use
  `Intl.DateTimeFormat("en-US", {timeZone: "America/New_York"})` so the
  browser also handles DST correctly.

### `teamagent/tests/test_market_hours.py`
- Updated existing tests to be DST-aware (May 2026 = EDT, so close = 21:00
  UTC; December 2026 = EST, so close = 22:00 UTC).
- Added explicit winter (`Dec 11`, `Dec 13`) test cases for both edges.
- All 50 tests pass; 9 stability_forecast tests still pass.

## Verification

```
$ curl -s https://fxinvestment-dhaftcbe.fly.dev/api/market-status \
    | python -c "import json,sys; d=json.load(sys.stdin); \
                 print(d['seconds_until_open'], d['next_event_utc_plus_5'])"
231 2026-05-04 02:00 (UTC+5)
```

i.e. forex opens in ~4 minutes (matches TradingView), local time 02:00
(UTC+5). Bug eliminated.

## Trade-mechanics question (also answered)

User asked: "Will trades open automatically at 70 %? On all forecasts /
all sessions?"

**Yes** — paper_trader opens a trade as soon as `forecast.probability_pct ≥ 70`
on any of the 28 pairs, on any session (free 70 % gate, set 2026-05-01).
But there are still these auxiliary blockers (which are the right thing
to keep):

- High-impact news ±30 min — skipped.
- Correlation cluster — `MAX_SAME_CURRENCY_BLOCK = 2`. Even at 70 %+, if
  there are already 2 open trades that share a base currency, the third
  one is rejected (kills macro-shock cluster losses).
- Strict ensemble disagreement — when ≥4 of the top 5 variants vote one
  side and the forecast says the opposite, the trade is rejected.

Trade-mechanics summary added verbatim into the user-facing message.

## Open follow-ups

- Verify on phone in ~22:00 UTC (Sunday open in EST winter test) — the
  test suite covers it but we should also verify visually.
- The cinematic /system header shows raw UTC ISO timestamps. Could also
  surface UTC+5 there if user wants it everywhere.
- Pollinations.ai narrative occasionally wraps paragraphs in markdown
  asterisks; minor cosmetic.
