# 2026-05-04 — Unified СДЕЛКИ hero section on main page

- **Session:** [4c057b34d737408786b3970ddd46fbba](https://app.devin.ai/sessions/4c057b34d737408786b3970ddd46fbba)
- **Branch:** `devin/1777859019-main-trades-section`
- **PR:** https://github.com/Jony-wws/Forex-wws2277/pull/11
- **Static deploy:** https://static-build-ftaqiznm.devinapps.com/

## What the user asked (verbatim)

1. *"Провер всё репозиторий и review devin и продолжает работать с того места где
   оставился https://static-build-bvmioctj.devinapps.com/ Если нужно изменить url
   не забудьте дай дат мне сыилку на сайте и ещё сделать фото что всё работает
   только фото 5"*
2. *"Я хочу чтобы ты разработал стратегию для каждой валюты чтобы он товар
   минимум 70% винрейта"* — explicitly stated as a SECOND task to do AFTER the
   first one.
3. *"я хочу на главной экране один единственный место где будет собрано все
   истории сделки и все открытые сделки только одна мест на отдельном разделе"*
4. *"я хочу что бы посе того как система будет давать прогноз он должен и сам
   должен открыться сделки на своем системе"* — auto-opening on prognosis ≥ 70%
   gate is already in `paper_trader.py` since 2026-05-01 (free 70% gate, see
   AGENTS.md rule #7); now it's surfaced on the main page in real time.

## What was done

### Code changes (PR #11)

- `teamagent/dashboard/static/intent.html` — added new section
  `<section id="main-trades-section">` directly under the tab nav, BEFORE the
  existing `final-signals-section`. Six summary tiles + three subsections
  («Открыты сейчас», «Последние закрытые», «Win Rate по парам»).
- `teamagent/dashboard/static/intent.css` — appended ~125 lines of `mt-*`
  prefixed styles (no collisions with `fs-/la-/dt-/fx-/ai-` prefixes).
- `teamagent/dashboard/static/intent.js` — appended `refreshMainTrades()`
  function (~200 lines) that fetches `/api/stats`, `/api/open-trades`, and
  `/api/closed-trades?limit=200` in parallel, computes live pips for open
  trades from `state.forecasts`, renders all 3 subsections, refreshes every
  30 sec. Auto-runs on script load.
- Total diff: +356 lines, no removals.

### Deploys

- Built static mirror: `bash scripts/build_static_mirror.sh` → 2.5 MB,
  155 baked JSON files.
- Deployed: `deploy frontend dir=static_build` →
  `https://static-build-ftaqiznm.devinapps.com/` (Cloudflare CDN).

### Tests

- 4 named test cases, all passed (see `test-report-pr11.md` in the repo
  root). Tile values exactly match baked `/api/stats.json`:
  Всего=10 / Открыто=0 / WIN=6 / LOSS=4 / WR=60.0% / PnL=+$1.80.
- 5 full-screen screenshots + screencast attached to user's chat and to PR
  #11 as a runtime test-results comment.

## Current state

- **Live deploy URL (this session):** `https://static-build-ftaqiznm.devinapps.com/`
- **Permanent URL (Fly.io, intermittent):** `https://fxinvestment-lbtxlhtb.fly.dev/`
- **PR #11:** open, ready for user review, no CI on this repo so nothing to
  wait on.
- **Auto-open behavior:** unchanged — paper_trader still opens at probability
  ≥ 70%; the new section just makes that visible on the main page.
- **WR is currently 60% on 10 trades** (6W / 4L). Insufficient sample for the
  follow-up 70% target — that's the next task.

## Open TODOs for the next session

The user's SECOND task (explicitly stated for after this PR):

> "я хочу чтобы ты разработал стратегию для каждой валюты чтобы он товар
> минимум 70% винрейта … для каждой валюты для каждой сессии … 5 прогноз на
> каждый валюти каждый день и минимум win rate"

**Plan for that follow-up:**

1. Run `python -m teamagent.strategy_search --top 10` against fresh 365-day
   Yahoo data per (pair × session × regime) — currently the saved
   `strategy_config.json` shows only 30 / 112 cells qualifying at the strict
   ≥70% Wilson lower-bound bar; need to push toward 112 / 112.
2. For pairs that can NEVER hit 70% on baseline indicators, add per-pair
   fundamental tilt (FRED + COT — already wired via `fundamentals.py` and
   `cot.py`) as a hard side-bias.
3. For sessions that under-perform (e.g. quiet Asia on EUR-pairs), introduce
   session-specific variants v211-v250 (already in strategies.py since
   2026-05-03) and re-run the sweep with `--variants asia-only`.
4. Stretch goal: 5 forecasts/day/pair (28 × 5 = 140 forecasts/day). Currently
   `forecast_scanner` runs every 5 min so the rate is naturally high; the gate
   is the 70% probability cap, not throughput.

This will be a multi-day work block — the user is aware and OK with that
("ты должен сделать большую работу это очень важно").

## Caveats / notes

- The legacy `/trades.html` page still exists but shows loading-state
  placeholders on the static mirror because the static-build script doesn't
  bake the script-driven endpoints it uses. This is **not a regression** —
  it was already that way before this PR. The user's request was to put
  everything on the main page, which is what this PR delivers.
- Did NOT lower the 70% gate (forbidden by AGENTS.md rule #7 without
  explicit user permission).
- Did NOT introduce simulators or fake data (forbidden by AGENTS.md rule
  #2).
- Did NOT add a second meta-voting endpoint (forbidden by AGENTS.md rule
  #3).
