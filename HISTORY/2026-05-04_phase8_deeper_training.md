# 2026-05-04 (cont) — Phase 8: deeper training

## Verbatim user quotes

> «Ты почему оставился ты же знаешь мою идею так сделай до конца и потом
> обучи систему»

> «Я тебя запрещаю не давать на всех валютах минимум 80% успешности и
> честно должно быть 80% успешности»

> «Я хочу чтобы ты полностью обучал систему понимаешь ты тут не важно
> бэк теста или стратегию тут важно только то что ты обучал знания то
> есть он будет соблюдать эти правила которые ты будешь написать ему»

> «не для того что бы уменьшить количество прогноз ты что не понимаешь
> тут важно чтобы ты обучал систему не ограничивали его с правилами а
> обучать»

> «У меня 80% уже полная работа сколько % будет забрать важно что бы ты
> работал полный»

## What was done

After Phase 7 (PR #17) which added the basic event-attribution boost
(±8 max), the user pushed back: he wants the system **trained**, not
filtered. Don't reduce forecasts — make the system smarter so
probability is more accurate, especially around real events.

Phase 8 extends the live integration with three deeper learning layers.

### 1. Expanded event archive (Phase 8a)

`teamagent/events/fred_global_calendar.py` — new module pulling
non-USD macro series from FRED OECD MEI:
- EUR / GBP / JPY / AUD / CAD / NZD CPI
- EUR / GBP / JPY / AUD / CAD / NZD unemployment / employment

Country-specific release-day approximations (UK CPI = 3rd Wed of next
month at 06:00 UTC, JP CPI = last Friday at 23:30 UTC, etc.).

Result: archive grew from 667 to **718 events**. Re-ran detector,
attribution, traps, profile — now have 13 persistent drivers (was 11)
and 260 (pair × session × event) cells (was 229).

### 2. Trained-rules generator (Phase 8b)

`teamagent/events/training.py` — reads all Phase-1..6 artefacts and
produces `state/learned_rules.json`:

- **17 high-conviction rules**: (pair × session × event_type) cells with
  concordance ≥ 75% on ≥ 4 historical instances. These are the signals
  the system has *learned* to trust — e.g. GBPUSD/NY/cb_rate_decision
  has 6 historical FOMC instances with 83% concordance "USD up" → SELL
  GBPUSD with high confidence.
- **112 pair-session bias cells**: persistent directional drift over the
  full 365 days. CADCHF/NY had 81% concordance ↓ (mean −5.5pip) — a
  consistent NY-session downward drift.
- **13 persistent driver event-types** copied from the analysis.

### 3. Three new live functions (Phase 8c)

In `teamagent/events/live_weights.py`:

| Function | Max boost | When it fires |
|---|---|---|
| `learned_rule_score()` | ±16 | one of 17 high-conviction events in ±6h window |
| `pair_session_bias_score()` | ±2 | always fires when concordance ≥ 70% / n ≥ 100 (no event needed) |
| `multi_event_cluster_amplifier()` | ±8 | ≥ 2 persistent events agree on direction in same window |

Plus session-name compatibility: `_norm_session()` and
`_hour_to_analysis_session()` map runtime session names (Asia /
London / LON+NY / NY / Off) to analysis session names (Asia / London
/ Overlap / NY) covering all 24 hours.

### 4. forecast_scanner BLOCK L (Phase 8d)

Three new vote calls inside `evaluate_pair()`:
```python
sess_now = ev_lw._hour_to_analysis_session(now.hour)
vote("learned_rule",       learned_rule_score(...))
vote("pair_session_bias",  pair_session_bias_score(...))
vote("multi_event_cluster", multi_event_cluster_amplifier(...))
```

All three contribute to score (and thus probability), never block.
AGENTS.md rule #7 (free 70% gate) preserved.

### 5. Verification (Phase 8e)

```
GBPUSD NY @ 2025-09-17 18:30 UTC (FOMC):  learned_rule = -10
USDCHF NY @ same:                          learned_rule = +10
USDJPY NY @ same:                          learned_rule = +10
EURUSD NY @ same:                          learned_rule = -10

CADCHF NY (no event):  pair_session_bias = -2
NZDCHF NY (no event):  pair_session_bias = -2 + trap_filter = +1

Live scan_all_pairs at 23 UTC (current time):
  8 / 28 pairs at ≥ 70% probability gate
  Top: EURUSD SELL 77.3%, USDCHF BUY 76.4%
```

## What this is NOT

- NOT a guarantee of 80% WR on every pair. The user explicitly forbade
  saying "this isn't possible" but I documented the realistic range in
  the PR #18 description. Honest expectation: +5-8pp on the 7 pairs with
  high-conviction rules during events; +1-2pp constant nudge on 70+
  pair-sessions with persistent drift; +4-8 extra during multi-event
  clusters. On pairs where the historical edge was 51% there's nothing
  to amplify.
- NOT a hard filter. Free 70% gate intact. Pair_session_bias is small
  enough (±2) that it can't push a 50% probability above the 70% gate
  on its own — it only refines what's already a real signal.
- NOT a backtest. This is training data fed into live decision-making.

## Three open PRs

- **PR #16**: 365-day event-attribution analysis (CSV + 28 markdown profiles)
- **PR #17**: Phase 7 — basic event boost + soft trap filter
- **PR #18**: Phase 8 — non-USD events + 17 learned rules + persistent bias + multi-event cluster

Merge order: #16 → #17 → #18. Each PR is based on the previous so
GitHub will show the right cumulative diff.

## Open TODOs (none required)

- User to merge PRs #16, #17, #18 in order.
- Optional later: tighten learned_rule thresholds (now concordance≥75%,
  could go ≥80% for even higher conviction at the cost of fewer rules).
- Optional later: add per-bar indicator-state to learned rules (e.g.
  "FOMC + RSI > 60 + MACD bullish → 90% WR" — nontrivial, requires
  replaying 365d at hour-level with all indicators).
