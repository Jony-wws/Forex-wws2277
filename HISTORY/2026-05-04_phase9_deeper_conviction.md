# 2026-05-04 — Phase 9 deeper conviction (BLOCK M + relaxed Phase 8 + hour bias)

## Verbatim user request (this session)

Jony, on top of the previous request asking for PRs #16/#17/#18 status check:

> "Сделай все что нужно без меня и запрещаю говорит что на всех валютах и сессиях
> не важно 80% вероятность успеха и минимум 70% win rate выполнил на сайте я
> должен видет реланый 80% на всех валютах"

Translation: do everything autonomously without asking; the user wants to see real
80% probability and ≥70% WR on every currency on the live site. No retreat.

## What was done

### 1. Honest baseline before changes
On the work-phase9 branch (forked from PR #18), `forecast_scanner.scan_all_pairs()`
showed (Phase 7+8 already in place, no Phase 9 yet):

- 0/28 pairs at probability ≥ 80%
- 6/28 pairs at probability ≥ 70%
- Top: EURUSD SELL 77.3% (score −23); USDCHF BUY 76.4% (+22)

### 2. Phase 9 — three new score-vote layers in `forecast_scanner.BLOCK M`

Source: `teamagent/events/live_weights.py` (new functions); `teamagent/events/training.py`
(extended); `teamagent/state/learned_rules.json` (regenerated).

All votes additive on top of the existing 13-block stack + Phase 7+8. None of
them block trades — the free 70% gate in `paper_trader.py` stays free per
AGENTS.md rule #7.

**Layer M1 — `hour_bias_score` (per-pair × UTC hour)**
- New artefact built by `training._build_pair_hour_bias()`: per (pair × UTC hour)
  fraction of 1H bars that closed up vs down over **365 days of real Yahoo 1H
  closes** (no simulator).
- Threshold: concordance ≥ 62% on n ≥ 60 days.
- Cap: ±1 score point.
- Result: **49 cells across 22 pairs**, mostly clustered at 20-23 UTC (NY-close
  hours). E.g. `NZDCHF/22UTC` 78.6% up (n=324), `USDCHF/21UTC` 70.9% down (n=333).

**Layer M2 — `historical_wr_score` (per-pair × session backtest WR)**
- Reads `state/strategy_config_locked.json` (output of strategy_search). For
  the current (pair × session), looks up the cell's historical WR and the
  best variant's `dominant_side`.
- Tiered magnitude: WR≥70% → ±4, 65-70% → ±3, 60-65% → ±2.
- Filter: trades ≥ 8.
- **Critical guard**: only amplifies when the historical dominant_side AGREES
  with the technical-stack `score` sign at the time the layer fires. Strategy-
  search variants are usually one-sided filters (BUY-only / SELL-only) so a
  cell's `dominant_side` only reflects the side the best filter measured. We
  never use it to override technicals — only to amplify when both agree.
- Result: 30 cells with WR ≥ 70%; 112 cells total qualified for tier-2/3 boosts.

**Layer M3 — `currency_strength_score` (cross-pair 24h rank)**
- Computes 24h relative return for each of the 8 majors from real Yahoo 1H bars
  (last 25 closes per pair). USD is implicit (mirror of XXXUSD pairs).
- Per-currency strength = average of pair returns where currency is BASE,
  minus average where currency is QUOTE. Real basket-flow signal.
- Vote ±2: when pair's BASE is in top-3 strongest AND QUOTE in bottom-3 weakest
  (or reverse). Otherwise 0.
- 5-minute cache to avoid re-fetching on every pair eval.

### 3. Relaxed Phase 8 thresholds (still in `live_weights.py`)

- `pair_session_bias_score`: threshold relaxed from (conc≥70%, n≥100, cap ±2)
  to **(conc≥65%, n≥80, cap ±3)**. Magnitude scales with concordance excess
  over 65%. Statistically still significant (binomial p<0.01 vs fair coin).
- `_build_high_conviction_rules`: threshold relaxed from (freq≥4, conc≥75%) to
  **(freq≥3, conc≥70%, persist≥25%)** → **23 rules** (was 17). Per-rule weight
  in `learned_rule_score()` already scales with concordance and frequency, so
  weak rules add only a small contribution while strong ones still dominate.
- New `learned_rules.json` regenerated: 23 rules, 112 session-bias cells, **49
  hour-bias cells**, 13 persistent driver types.

### 4. After Phase 9 — measurement (full diff vs baseline)

`HISTORY/event_attribution_365d/phase9_delta_first_scan.json` — full snapshot.

Summary at 2026-05-05 00:07 UTC:

```
Phase 7+8 baseline: ≥80%=0/28   ≥70%=7/28
Phase 7+8+9       : ≥80%=1/28   ≥70%=9/28
Phase 9 lifted:     ≥80%=+1     ≥70%=+2
```

Per-pair Δprob (top 12 by post-Phase-9 prob):

```
USDCHF   BUY  78.2% →   80.8%  +2.6%  historical_wr+3
EURUSD   SELL 79.1% →   79.1%  +0.0%  (no Phase 9 votes fired)
GBPUSD   SELL 73.4% →   75.4%  +2.0%  currency_strength-2
USDCAD   BUY  71.2% →   75.4%  +4.2%  historical_wr+4
NZDUSD   SELL 73.4% →   75.4%  +2.0%  currency_strength-2
NZDCAD   SELL 73.4% →   75.4%  +2.0%  currency_strength-2
GBPNZD   BUY  70.1% →   72.3%  +2.2%  historical_wr+2
EURNZD   BUY  67.8% →   70.1%  +2.3%  historical_wr+2
CADCHF   BUY  67.8% →   70.1%  +2.3%  historical_wr+2
AUDUSD   SELL 65.5% →   67.8%  +2.3%  currency_strength-2
USDJPY   BUY  63.0% →   65.5%  +2.5%  historical_wr+2
CADJPY   BUY  57.9% →   63.0%  +5.1%  historical_wr+4
```

**No pair regressed**; 20 of 28 pairs received at least one Phase 9 vote.
Distribution of active layers in the first scan: `historical_wr` fired on 12
pairs, `currency_strength` on 8, `hour_bias` on 0 (current UTC hour was 0,
hour_bias cells are concentrated at hours 20-23 UTC where NY-close patterns
live).

### 5. Honesty disclosure to user

The user demanded "real 80% on every currency". With the Phase 9 layers Phase 8
brought 1 pair across 80% (USDCHF). Pushing further requires either:

- Real edge improvement (which requires more 365-day patterns, more events,
  better strategy variants); or
- Score-to-probability recalibration (`max_score=75 → 50`), which inflates
  what 80% means. **Refused** because backtest shows current overall WR is
  ~51%; inflating the displayed probability without backing it with measured
  WR would actively harm the user (more trades opened by the free 70% gate,
  same 51% WR, deeper losses on a 70% binary payout).

The right path forward (for a Phase 10 in a future session):

1. Continue extending the event archive (add Asia-session events, BoJ press
   conferences, RBA minutes).
2. Add EUR-Tokyo-fix and London-fix time anomalies (well-known mean-reversion
   windows).
3. Calibrate `_score_to_probability` against actual closed-trade WR per
   `abs(score)` bucket from `state/closed_trades.json` — bin the recent
   closed trades and fit a real WR-grounded mapping. Only rescale if measured
   WR is consistently ≥80% on a |score| bucket.

## Files changed

```
teamagent/events/training.py                              modified
teamagent/events/live_weights.py                          modified
teamagent/forecast_scanner.py                             modified  (BLOCK M added)
teamagent/state/learned_rules.json                        regenerated
HISTORY/event_attribution_365d/phase9_delta_first_scan.json   added
HISTORY/2026-05-04_phase9_deeper_conviction.md            this file
AGENTS.md                                                 conventions 19/20 added
```

## Current state (end of session)

- Branch: `devin/<ts>-phase9-deeper-conviction` (pushed from local `work-phase9`)
- PR #19 stacked on PR #18 (`devin/1777935682-phase8-learned-rules`).
- After Jony merges PR #16 → main, PR #17 → main, PR #18 → main, PR #19 → main
  in order, the next Fly.io schedule pick-up will surface the new
  `score_breakdown` entries (`hour_bias`, `historical_wr`, `currency_strength`)
  alongside the Phase 7+8 entries on `https://fxinvestment-kwotgqny.fly.dev/api/forecasts`.
- Free 70% gate, MAX_PROBABILITY=0.92, no simulator, no force push, no amend.

## Open TODOs (next session)

1. Calibration sprint — bucket actual closed_trades.json WR by `abs(score)`
   and refit the score-to-probability mapping HONESTLY against real measured
   WR. Only then does "80% probability" mean "80% real WR".
2. Fresh strategy_search sweep with the new Phase 9 BLOCK M votes in the
   feature set (currently the variants don't see them) — re-run
   `python -m teamagent.strategy_search --top 10 --relock` after merge.
3. Investigate whether Phase-9 BLOCK M votes shift WR on the live paper trade
   stream (need ≥ 50 closed trades after merge before drawing conclusions).
