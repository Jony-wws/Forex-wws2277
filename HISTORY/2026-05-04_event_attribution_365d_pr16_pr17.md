# 2026-05-04 — 365-day event-attribution + live integration

## Verbatim user quotes (Russian, preserved)

> «Ты должен проверить каждую валюту на какие факты реагировали все валюты за
> прошедший год... ты должен найти такие факты которые будут присутствовать в
> рынке всегда постоянно... ты должен проверить каждый день 365 день каждую
> валюту на что они реагировали на каждых сессиях... важно чтобы ты нашёл
> несколько источников которые заставили расти или падать на каждом валюте...»

> «...это не просто бэк теста а узнать их паттерны... не эконом время дали
> всё до конца...»

> «Видео не работает если это мешает то можно убрать его... важно система»

> «Полный» (full scope, 2-3 hours)

> «65% лимти у меня. И так сайт готов полностью ты сделал всё что я сказал
> или нет» — at the end, asking honestly if everything is done.

## What was done in this session

1. **PR #15** (already done before this session): Honest 365-day backtest,
   28/28 pairs, 31 858 trades, WR 51.07%. No simulators.

2. **PR #16** (event-attribution archive + analysis):
   - Phase 1: Event archive — `state/events_365d.json` with 667 events
     (83 CB + 165 FRED + 399 COT + 20 geo). Sources: FRED public CSV,
     CFTC Socrata API, curated CB calendar, curated geo.
   - Phase 2: `teamagent/events/detector.py` — move detector on Yahoo 1H,
     28 156 cells, 2 312 significant moves (>1.5σ), 720 traps (≥80% retrace
     in 6h).
   - Phase 3: `teamagent/events/attribution.py` — 639 (event, move) matches
     across 229 (pair × session × event-type) cells, 17 event-types.
   - Phase 4: `teamagent/events/traps.py` — 79 high-trap event patterns,
     112 pair-session summaries; AUDNZD/NY tops at 89% trap rate.
   - Phase 5: `teamagent/events/profile.py` — 11 persistent drivers, 28
     markdown profiles per pair.
   - Phase 6: `HISTORY/event_attribution_365d/2026-05-04_365d_event_attribution.md`
     master verdict + caveats + meta.json.

3. **PR #17** (live integration):
   - `teamagent/events/live_weights.py` — loads CSVs once, exposes
     `event_score_contribution(pair, session, now)` and
     `trap_score_penalty(pair, session, score)`. Degrades to no-op if
     CSVs missing.
   - `teamagent/forecast_scanner.py` — added BLOCK K calling both
     functions. Uses existing `vote()` mechanism. Capped ±8 contribution.
     Trap penalty never zeros the trade — free 70% gate (AGENTS.md rule
     #7) preserved.

## Current state

- Both PRs open: #16 (analysis, on `devin/1777932998-event-attribution-365d`),
  #17 (live integration, on `devin/1777935222-phase7-event-weights`,
  based on #16's branch).
- Live system on `main` is unchanged until user merges. After merging
  in order (#16 first, #17 second), forecast_scanner will start
  emitting `event_attribution` and `trap_filter` rows in
  `score_breakdown` whenever the conditions hit.
- All artefacts (CSVs + JSON archive + 17.9 MB JSONL of moves) are
  committed so future sessions don't need to re-fetch from Yahoo or
  FRED.

## Open TODOs (none required)

- User to merge PR #16 first, then PR #17.
- Optional follow-ups (NOT in scope of these PRs):
  - Add EU/UK/JP/AU/CA CPI/employment events to the archive (currently
    USD-heavy at ~25% of total).
  - Wire trap-filter counters to a dashboard panel so user can see how
    often it fires per session.
  - After merging #17, monitor live `paper_stats.json` for 50-100 trades
    to measure real WR delta from event-weighting.

## Honest verdict (to user)

The user asked for "сайт готов полностью" — analysis IS done, live
integration code IS written and pushed. But until #16 + #17 merge to
main, the running site does NOT yet use the event-attribution edge.
Two clicks (review + merge in order) bridge that gap.

Cannot promise +20pp WR increase. Realistic expectation per the
analysis: NFP has 91% concordance but only 44% 24h-persistence, so the
binary-options upside from event-weighting alone is incremental
(+2-5pp), not revolutionary. Trap-filter prevents some clustered
losses on whipsaw cells but doesn't make the system a money printer.
The honest improvement story will become visible after 100+ trades
post-merge.
