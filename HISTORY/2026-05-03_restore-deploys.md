# 2026-05-03 — Restore deploys + dependabot vulnerability cleanup

PR: <https://github.com/Jony-wws/Forex-wws2277/pull/7>
Branch: `devin/1777786798-restore-deploys`

User request (verbatim, RU):
> "Проверить все репозиторий и review devin и продолжает работать с того
> места сейчас сайт не работает не открывается я буду заходить через
> телефон андроид chrome по этому важно что бы сайт открился и проверить
> все увезвимоси и исправить их и что бы всё работало 100% всё и что
> всё было реланоим данные и что бы историю тоже короче всё что есть
> было восстановлено Прошлое работы сделки с много другое"

User confirmation mid-session:
> "Всё работает если убрал все увезвимоси то можно оставится если всё
> работает"

## What was broken

Both URLs from the previous AGENTS.md were dead:
- `https://fxinvestment-vsxcxrqj.fly.dev/` — Fly machine had auto-stopped and was no longer reachable (curl 30 s timeout).
- `https://static-build-seanlntw.devinapps.com/` — was the **pre-PR #6** build, so the System tab still threw `SyntaxError: Unexpected token '<'` on `/api/wr-floor`, `/api/fundamentals`, `/api/daily/closed-trades`, `/api/daily/paused`, etc., AND `intent.html` still loaded `lightweight-charts` from `unpkg.com` (which stalled on the user's mobile carrier on 2nd visit).

The PR #6 fixes had been merged but the build/deploy was never re-run.

## What was shipped

1. `pip install ...` then `bash scripts/build_static_mirror.sh` — 146 baked
   `/api/*.json` files.
2. `deploy(command="frontend", dir=".../static_build")` →
   **`https://static-build-fukmtgwy.devinapps.com/`** (instant, mobile-friendly).
3. `deploy(command="backend", dir="...", volume=true)` →
   **`https://fxinvestment-dhaftcbe.fly.dev/`** (live data, ~10 s cold start).
4. `AGENTS.md` updated with both new URLs (old URLs removed; future
   sessions reading the file will go straight to working URLs).

## Verification

- Static URL `/` → cinematic landing renders, EURCAD pair card opens
  deep-dive modal with full RSI/ATR/Wyckoff/Hurst/Order-Flow metrics.
- Static URL `/system.html` → History tab shows `10 сделок · 6 WIN ·
  4 LOSS`, first closed trade `EURNZD BUY 1.98620 → 1.98724` (matches
  `state/closed_trades.json`).
- Fly URL `/api/health` → HTTP 200, valid JSON, `as_of` 2026-05-03.
- User confirmed reachability from Android Chrome ("Всё работает").

## Vulnerabilities addressed (per user's "проверить все увезвимоси")

| Repo | PR | Fix |
|---|---|---|
| `Jony-wws/FOREX` | #1 | `vite` 6.4.1 → 6.4.2 (path-traversal in optimize-deps sourcemap handler + server.fs check on env transport) — `@dependabot squash and merge` directive issued |
| `Jony-wws/FOREX21` | #1 | identical vite 6.4.2 bump — same directive issued |
| `Jony-wws/Forex-wws2`, `Forex-wws22`, `Forex-wws27` | — | repos contain only `README.md` + `SESSION_STATE.md` (cross-session mirror only, no executable code, no `package.json`) |
| `Jony-wws/Forex-wws2277` (canonical) | — | Python only; Fly.io deploy uses `pyproject.toml` + `uv sync`; no JS supply-chain vector |

## Open TODOs

- Wire `bash scripts/build_static_mirror.sh && deploy frontend` into the
  hourly Devin Schedule so the static mirror auto-refreshes its baked
  state every hour (currently it freezes at deploy time).
- Verify the dependabot auto-merge actually fired on `FOREX` PR #1 and
  `FOREX21` PR #1 (the bot will only merge if auto-merge is enabled
  org-wide for the user's account — needs manual confirmation).
