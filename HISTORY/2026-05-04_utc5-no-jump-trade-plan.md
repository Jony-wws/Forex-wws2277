# 2026-05-04 — UTC+5 + scroll-no-jump + 5h trade plan + price freshness

## User requests (verbatim, in order)

1. *"Сайт не открывается у меня сделай что-нибудь что бы быстро открился но при
   этом всё работало"* — site times out / OOM-killed.
2. *"utc 5+ система должна работать по этой системы и всё данные должны быть на
   это время актуально"* — show times in UTC+5.
3. *"я проверил что tradingwiew на всех валюти другой текущая цена а на сайте
   другой эту должно исправить 100% должен быть точный"* — price differs from
   TradingView, must be more accurate.
4. *"когда система обновляется сайт мне перекидывает на главной странице вот
   например я смотрю какой то информация на сайте через 10 с каждым раз при
   обновлении он прикидывает меня на начало страницы так не удобно"* — page
   scrolls back to top every 10 sec on auto-refresh.
5. *"система не напсал в прогнозе на сколько времени открыть сделку и система
   должна понимать что его прогноз должен отработать за 5 часов и он должен
   проверит что будет приходить за 24 часа заранее"* — show 5h trade duration
   explicitly, look 24h ahead at news/decisions.
6. *"то же они хотят" / "главное всё равно будут то что хотел рынка и игрокы"* —
   24h institutional decision is locked until 00:00 UTC+5; everything inside
   the session is noise.

## Fixes shipped

### A. OOM / site doesn't open  *(commit `a984c31`)*
Root cause: dashboard ran in-process `_fly_state_refresher` (`scan_all_pairs`
every 10 min) and `_fly_paper_trader_tick` (every 60s) on the 256-MB Fly
machine — got OOM-killed every minute, machine kept rebooting, site timed out.

Fix: flipped both tasks to opt-IN (require `FLY_DASHBOARD_REFRESH=1` /
`FLY_PAPER_TRADER=1` to enable). Devin Schedule
`sched-5229cad67c5e4965aa6400ba6da8070a` already refreshes state every 30 min
on the Devin VM and redeploys Fly with fresh state — no in-process scanning
needed.

Verified: 5 consecutive `curl` pings to `/` come back HTTP 200 in 0.16-0.22 s.
Logs now show `[fly-refresh] skipped (default off)` instead of OOM crashes.

### B. UTC+5 clock + UTC+5 timestamps  *(commit `e2b5cb2` / rebased `4411be8`)*
- Topbar now shows `HH:MM:SS UTC+5  ·  HH:MM:SS UTC` (live, refreshed every
  second).
- `forecast_as_of` shown in UTC+5 instead of UTC.
- Trade-plan expiry shown as `HH:MM UTC+5 · HH:MM UTC` so user can
  cross-reference both timezones.
- Helper functions `toUtc5()`, `fmtUtc5HM()`, `fmtUtc5HMS()` added to
  `stakan-only.js`.

### C. Price freshness — closer to TradingView  *(commit `e2b5cb2`)*
Yahoo is mid-rate; TradingView and broker feeds use bid/ask from different
LP — natural ±5-20 pip drift exists and cannot be fully eliminated without a
broker key. What we can do:
- Reduced 1m TTL from 60s → 15s in `data/yahoo.py` (live-price endpoint was
  delivering up-to-60-sec-stale bars; now it's 15s max).
- Live-price endpoint already exposes `bar_time` (UTC ISO of the last 1m bar
  used). UI now reads it and shows a `Yahoo · N с назад` badge in the
  pair-bar so user can see exactly how stale the price is.
- Tooltip on the freshness badge explains the natural mid/bid-ask drift.

### D. No-jump auto-refresh  *(commit `e2b5cb2`)*
Root cause: `renderOrderBook()` called `curRow.scrollIntoView({block:"center"})`
on every refresh. That scrolls every overflow ancestor including `<html>`,
which yanked the page back to the orderbook section.

Fix:
- Replaced `scrollIntoView` with manual `body.scrollTop = …` so only the
  orderbook container scrolls (not the page).
- Auto-scroll to current price now triggers ONLY when the selected pair
  changes (`state.lastOrderBookPair !== state.selectedPair`). On same-pair
  refresh, the user's scroll position inside the orderbook is preserved.

Verified visually: scrolled to mid-page, waited 30s through 3 refresh
cycles — page stayed put.

### E. 5h trade plan in verdict block  *(commit `e2b5cb2`)*
New "ПЛАН СДЕЛКИ" panel inside `.so-verdict`, with explicit:
- Открыть на: `5 часов (бинарный)`
- Истечёт в: `HH:MM UTC+5 · HH:MM UTC`
- Цена сейчас: live price
- Цель к 00:00 UTC+5: from `target_by_midnight` (computed in stakan_view)
- ⚠️ Новости впереди: top high-impact event in 5-24h window with countdown
  `через Xч (в HH:MM UTC+5) · N в 5ч / M в 24ч`. Hidden when no events.

### F. 24h-horizon language in verdict reason  *(commit `e2b5cb2`)*
Updated `_institutional_verdict()` reason_ru for all three strength tiers to
explicitly state the user's mental model:
- strong: *"На горизонте 24ч до 00:00 UTC+5 рынок выбрал направление ВВЕРХ —
  мелкие колебания внутри сессии это шум, прогноз отработает за ~5 часов."*
- medium: *"направление до 00:00 UTC+5 есть, но запас прочности средний.
  Прогноз отработает за ~5 часов."*
- weak: *"но направление всё-таки ВВЕРХ (24ч-горизонт). Прогноз ~5 часов."*

### G. CSS hidden bug  *(commit `2a946c7` / rebased `b7bba62`)*
`.so-tp-row { display: flex; }` overrode the `hidden` HTML attribute, so the
"Новости впереди" row stayed visible with placeholder "—" even when there
were 0 events. Fixed with `.so-tp-row[hidden] { display: none !important; }`.

## Permanent URL & schedule

Live: `https://fxinvestment-ytjmvlnz.fly.dev/`
Schedule (refreshes data every 30 min): `sched-5229cad67c5e4965aa6400ba6da8070a`

## Verification (current state)

- 28 pairs all show КУПИТЬ/ПРОДАТЬ/СКОРЕЕ-/ВОЗМОЖНО-, never ОЖИДАНИЕ.
- All probabilities in [70, 92] %.
- Verdict reason mentions "24ч-горизонт" and "5 часов".
- Topbar clock shows both UTC+5 and UTC.
- Trade plan panel shows 5h expiry in UTC+5.
- Price freshness badge "Yahoo · N с назад" updates each second.
- Auto-refresh does not scroll the page back to top.
- Site responds in ~200 ms (no OOM crashes).

## Open TODOs / known limitations

- Yahoo mid vs broker bid/ask drift (~5-20 pips) cannot be eliminated without
  a paid LP feed. UI now makes this transparent via the freshness badge +
  tooltip.
- ForexFactory RSS sometimes 429-rate-limited; news-watch endpoint still
  works once cache populates.
- Devin Schedule `sched-5229cad67c5e4965aa6400ba6da8070a` already created and
  approved — runs `*/30 * * * *`.

## Files changed

- `teamagent/dashboard/server.py` — opt-IN flag for in-process refresh tasks
- `teamagent/data/yahoo.py` — 1m TTL 60s → 15s
- `teamagent/dashboard/static/stakan-only.html` — trade-plan markup +
  freshness badge slot
- `teamagent/dashboard/static/stakan-only.css` — trade-plan styling +
  hidden-attribute fix
- `teamagent/dashboard/static/stakan-only.js` — UTC+5 helpers, no-jump
  scroll, trade-plan render, news-watch render, bar_time freshness
- `teamagent/stakan_view.py` — reason_ru with 24ч/5ч language
