# 2026-05-03 — Real-time market-status fix

PR: https://github.com/Jony-wws/Forex-wws2277/pull/9
Branch: `devin/1777843807-real-time-market-fix`
Static mirror: https://static-build-bvmioctj.devinapps.com/
Live Fly backend: https://fxinvestment-lbtxlhtb.fly.dev/

## Verbatim user complaints

> Провер всё репозиторий и review devin и продолжает работать над системой
> которая создал devin. Система напсат что рынок закрыт и система показывает
> время но все равно не понимает что рынок открыт и сделки не открываются —
> всё что есть на сайте должен обновить в реальном времени данные а то
> прогноз будет не точный
> https://static-build-fukmtgwy.devinapps.com/
> Система вообще не понимает что рынок открыт исправ он должен понимать всё
> понимаешь когда что нужно сделать и всё он должен получить данные реально
> времени.

User accessed an old static-build URL (`fukmtgwy`) on Android Chrome.
The page showed "🔴 ЗАКРЫТ" even though Forex is actively open
(Sunday 21:20 UTC = Sunday 17:20 EDT in New York, well after the
17:00 NY weekly open).

## What was wrong

1. **Frozen `market-status.json`.** The static mirror baked all
   `/api/*` responses as JSON files at deploy time. The build that the
   user opened had `is_open=false` and stale `as_of_utc` from the
   instant of the bake, so the page lied about the market state until
   the next redeploy.
2. **Buggy day-anchor calculator.** Once the shim was rewritten to
   synthesize market-status client-side, `_nextNyAnchor()` had a
   day-iteration bug: it preserved hour-of-day while incrementing days,
   and on Sunday-after-open `ny.hour < 17` was never satisfied for any
   future Friday, so it skipped 14 days ahead and the badge read
   "закроется через 14д 0ч 0м".
3. **Header badge was invisible.** It was being appended to
   `.fs-multi-header` which sits inside a section that renders zero
   height on this layout, so the user saw nothing despite the badge
   being correctly created and updated in the DOM.
4. **Fly backend was unreachable.** The previously-stamped Fly URLs
   (`fxinvestment.fly.dev`, `fxinvestment-dhaftcbe.fly.dev`,
   `fxinvestment-vsxcxrqj.fly.dev`) all rotated away. Only one machine
   was current: `fxinvestment-lbtxlhtb.fly.dev`. CORS was missing too,
   so even when the live backend was up the static mirror couldn't
   reach it from `*.devinapps.com`.

## What was done

- Rewrote `teamagent/dashboard/static/static-shim.js` to:
  - Synthesize `/api/market-status` client-side (DST-aware NY tz
    conversion via runtime `Intl.DateTimeFormat`).
  - Live-first proxy for all other `/api/*` endpoints (4.5s timeout,
    fallback to baked, response tagged with `X-FX-Source: live|baked`).
  - Hardcoded `LIVE_BACKEND = https://fxinvestment-lbtxlhtb.fly.dev`
    with optional `window.FX_LIVE_BACKEND` runtime override.
  - Fixed `_nextNyAnchor()` — now iterates 14 calendar days and picks
    the first 17:00 NY moment strictly in the future.
- Added `CORSMiddleware` to `teamagent/dashboard/server.py` allowing
  `*.devinapps.com` and `*.fly.dev` origins, exposing `X-FX-Source`.
- Refactored `teamagent/dashboard/static/intent.js`:
  - New `ensureMarketBadgeEl()` hosts the badge in `.fx-toolbar`
    (always-visible top bar) with `.fs-multi-header → body` fallback.
  - `refreshLiveMarketBadge()` polls every 1 second; on network
    failure synthesizes from `window.FX_clientMarketStatus()`.
  - New `ensureStaleBanner()` for snapshot-vs-clock disagreement.
- Added regression tests in `teamagent/tests/test_market_hours.py`
  for the user's exact moment of complaint (2026-05-03 21:20 UTC).
  All 146 tests pass.
- Redeployed Fly backend and static mirror; updated AGENTS.md and
  fly-deploy SKILL.md with the new `fxinvestment-lbtxlhtb` URL.

## Visual verification

Recorded a screencast (`rec-1d67e4d1.mp4`) at 22:08–22:10 UTC showing:

- Badge "🟢 РЫНОК ОТКРЫТ · закроется через 4д 22ч 51м · live"
  rendered in the top toolbar.
- Countdown decrements: 51м → 50м → 48м across 2 minutes.
- Per-pair card detail (EURUSD): "Рынок открыт · До закрытия 118ч 54м"
  (~5 дней — agrees with the global badge).
- Source tag is "live", meaning the proxy reached Fly successfully.

## Current state

- Branch `devin/1777843807-real-time-market-fix` is pushed and PR #9
  is open. No CI on this repo, but unit tests are green locally.
- Static mirror at `https://static-build-bvmioctj.devinapps.com/` is
  deployed with the bug-free build.
- Fly backend at `https://fxinvestment-lbtxlhtb.fly.dev/` is alive but
  free-tier-flaky (intermittent connection-resets under concurrent
  load). The live-first proxy gracefully falls back to baked snapshots
  on those failures.

## Open TODOs / followups

- Adversarial stale-banner test (inject dead `window.FX_LIVE_BACKEND`,
  verify yellow banner appears) was NOT executed in this session
  because the computer-use console action couldn't run with Chrome
  focused. Planning to write a Playwright/CDP script that does the
  same thing programmatically; commit it under
  `.agents/skills/adversarial-tests/`.
- Fly free-tier instability suggests we should:
  - Add a "mini-keepalive" (fetch `/api/health` every 60s from the
    static mirror) to prevent the worker from cold-starting.
  - Consider migrating to the Hobby plan or another always-on host.
- The intent.js header ICONS still emit malformed-looking
  `<span style=...>🎯/span>` in some readers — verified by `xxd` it's
  actually correct UTF-8 `</span>`; this was a terminal-rendering
  artifact only, not a real bug.
