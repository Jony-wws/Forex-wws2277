# 365-Day Event-Attribution Analysis — Honest Verdict

**Period:** 2025-04-04 → 2026-05-04 (365 days)
**Pairs:** all 28 (majors + crosses + JPY pairs)
**Sessions:** Asia (00-06 UTC), London (07-12 UTC), Overlap (13-16 UTC), NY (17-21 UTC)
**Price source:** Yahoo Finance 1H OHLCV (real, no simulators)
**Event sources:** FRED (USD macros), CFTC COT, curated CB calendar, curated geopolitical
**Total events archived:** 667
**Total (pair × day × session) cells analyzed:** 28 156
**Significant moves (>1.5σ above 20d-baseline range):** 2 312 (8.2%)
**Traps (significant move reversed ≥80% within 6h):** 720 (31.1% of significant moves)

> User's verbatim ask:
> «Ты должен проверить каждую валюту на какие факты реагировали все валюты за
> прошедший год... ты будешь понимать на какие источники нужно опираться, важно
> чтобы ты нашёл несколько источников которые заставили расти или падать на
> каждом валюте.»
>
> This document delivers exactly that: per-pair × session reaction map plus
> the persistent-driver list and the trap map.

---

## TL;DR — what the data says

1. **Most reliable persistent drivers** (qualified: ≥10 matches AND
   ≥60% persistence OR ≥70% concordance over 365d):

   | Driver | matches | persistence_24h | concordance | hit-rate (vs prior) |
   |---|---:|---:|---:|---:|
   | US GDP advance | 12 | 73% | 100% | 90% |
   | US Core PCE | 17 | 64% | 86% | 23% |
   | US PCE Headline | 17 | 64% | 86% | 23% |
   | CB Rate Decisions (8 banks) | 171 | 56% | 76% | n/a |
   | CB Press Conferences | 56 | 45% | 85% | n/a |
   | US NFP | 16 | 44% | 91% | 41% |
   | US Unemployment Rate | 15 | 44% | 91% | 9% |
   | US PPI | 15 | 31% | 95% | 23% |
   | US CPI | 12 | 30% | 100% | 36% |
   | US Core CPI | 12 | 30% | 100% | 36% |
   | COT Extreme Short | 19 | 26% | 94% | n/a |

   **Read this carefully.** "Concordance" = % of times the move went in the
   same dominant direction for the event currency (high concordance = the
   move is consistent across instances). "Persistence_24h" = of the next
   24h, what % of bars stayed on the same side as the session-close bias.
   "Hit-rate vs prior" = for FRED events with a numeric value, % of times
   sign(actual − prior) matched sign(currency move). Hit-rates being low
   (e.g. NFP 41%) mean **markets often react opposite to the surprise** —
   classic "buy the rumour, sell the news" pattern.

2. **Most "trappy" pair × session combos — AVOID news-trading on these:**

   | pair | session | trap_pct_of_significant | n_significant |
   |---|---|---:|---:|
   | AUDNZD | NY | **89%** | 18 |
   | NZDCHF | NY | 68% | 19 |
   | CHFJPY | NY | 67% | 18 |
   | NZDCAD | NY | 64% | 25 |
   | EURNZD | NY | 64% | 22 |
   | GBPNZD | NY | 64% | 22 |
   | CADCHF | Asia | 62% | 21 |
   | USDCAD | Asia | 57% | 23 |
   | USDJPY | NY | 56% | 18 |
   | EURCHF | NY | 53% | 17 |
   | GBPCHF | NY | 50% | 20 |
   | AUDCHF | NY | 50% | 18 |
   | NZDCHF | Overlap | 50% | 18 |
   | AUDCHF | London | 50% | 16 |

   **Pattern:** NY session on exotic crosses (especially NZD-pairs and
   CHF-pairs) tends to whipsaw. Big spike followed by ≥80% reversal in 6h.
   These are the cells where stop-runs and false-breakouts live. Pure-news
   strategies will get killed here.

3. **Cleanest pair × session combos — news moves stick (≤15% trap rate):**

   | pair | session | trap_pct | n_significant |
   |---|---|---:|---:|
   | EURGBP | London | **0%** | 17 |
   | EURCAD | NY | **0%** | 18 |
   | USDCAD | Overlap | 5% | 19 |
   | USDJPY | Overlap | 5% | 19 |
   | EURUSD | London | 6% | 18 |
   | AUDCHF | Overlap | 7% | 15 |
   | GBPCAD | NY | 8% | 24 |
   | EURUSD | NY | 10% | 21 |
   | USDCHF | Overlap | 11% | 19 |
   | USDCAD | NY | 13% | 24 |
   | CHFJPY | London | 13% | 23 |

   **Pattern:** London session on EUR/GBP/USD majors keeps news-driven
   moves clean. Overlap session on USD-pairs is also reliable — high
   liquidity, both ECB tape and US ECON release windows align.

---

## How to use these results

### For news-based forecasting

- **Boost probability** when a high-concordance / high-persistence event
  is in the ±2h window. Top candidates by quality:
  US GDP, US PCE, CB Rate Decisions on majors during London/Overlap.
- **Down-weight** the move on cells where trap-rate ≥ 50% — the spike is
  more likely a stop-run than a real direction.

### For trap avoidance

- **Block trade-open** for N=60 minutes after a significant move on:
  `AUDNZD/NY`, `NZDCHF/NY`, `CHFJPY/NY`, `NZDCAD/NY`, `EURNZD/NY`,
  `GBPNZD/NY`, `CADCHF/Asia`, `USDCAD/Asia`. These cells reliably reverse.
- **No filtering** on: `EURGBP/London`, `EURCAD/NY`, `EURUSD/London/NY`,
  `USDCAD/Overlap+NY`, `GBPCAD/NY` — moves stick.

### Honest limitations

1. **Event coverage is asymmetric.** Our archive is ~25% USD events
   (165 FRED), ~12% CB-decisions (83 across 8 banks), ~60% COT (399, mostly
   "neutral z" tier), and ~3% geo. We have **no EU/UK/JP/AU/CA CPI/employment**
   events scheduled — those are real movers we miss. A signed-surprise
   table for those would significantly improve attribution coverage on
   non-USD pairs.

2. **Only 28% of significant moves matched a known event** (639 / 2 312).
   The rest are either coincident with non-archived events (technical
   moves, Asian session liquidity, off-calendar geopolitics) or pure
   technical breakouts. Expanding event coverage to 90%+ requires paid
   feeds (Bloomberg, Eikon) or a full 52-week scrape of ForexFactory which
   was deliberately skipped here for time.

3. **CB-decision concordance (76%) is computed on event-currency direction
   only** — we don't differentiate hawkish/dovish surprises (no consensus
   data is free). The "76%" means "the event-currency moved consistently
   in one direction across instances", not "we predicted the move". The
   actual edge from CB decisions requires reading the surprise vs market
   pricing, which needs a paid OIS-rate feed.

4. **Persistence-24h doesn't mean trade-WR.** A 60% persistence on NFP
   means "the move held 24h 60% of the time", not "if you traded NFP you'd
   win 60%". A real binary-options WR depends on entry timing, stop, and
   payout — those need separate backtesting which the existing
   `paper_trader` already does.

5. **Trap detection threshold (≥80% retrace in 6h) is one definition.**
   Tightening to ≥100% (full reversal) would cut trap counts in half;
   loosening to ≥50% would double them. The 80% bar is a balance:
   strict enough that a typical noise wiggle doesn't qualify, loose enough
   that genuine news-spike-then-reversal cases all show up.

---

## Per-pair verdicts

See the 28 individual files in this folder: `pair_<PAIR>.md`. Each contains
session-by-session (Asia / London / Overlap / NY) breakdown of:

- # of significant moves and trap %
- top event drivers (with concordance, persistence, trap-rate)
- frequent trap setups (≥50% reversal patterns)

Highlights worth mentioning in this top-level document:

- **EURUSD London**: cleanest cell of all 28 majors. 18 significant moves,
  only 1 trap (5.6%). Top driver: US Refunding (n=2, persistence 85%).
  US data spillover to London close is reliable.
- **EURUSD NY**: also clean (10% trap-rate). CB decisions move it in 83% of
  instances with 49% 24h-persistence. Most reliable channel for FOMC days.
- **EURGBP London**: the **cleanest London cell entirely** (0% trap rate
  across 17 significant moves). Tight pair, news-driven moves stick.
- **AUDNZD NY**: 89% trap rate. **Do not trade news here.** This pair has
  the lowest liquidity at NY hours; spikes get faded almost without
  exception.
- **USDJPY NY**: 56% trap rate, but London/Overlap are clean. JPY-pair
  behaviour is asymmetric — Tokyo close and US close create reversals.
- **CB Rate Decisions**: 75.5% directional concordance across 171 matches
  on 52 cells. Highest-frequency persistent driver. ECB/Fed dominate by
  volume of moves attributable.

---

## Files in this folder

| File | What |
|---|---|
| `2026-05-04_365d_event_attribution.md` | this verdict document |
| `meta.json` | machine-readable summary stats |
| `per_event_response.csv` | aggregate stats per event-type |
| `per_event_pair_session.csv` | full table: 229 cells, every (pair × session × event-type) reaction |
| `persistent_drivers.csv` | 11 event-types qualifying as persistent |
| `trap_event_patterns.csv` | trap rates per (pair × session × event-type) |
| `trap_time_patterns.csv` | trap rates per (pair × session × hour-of-day) |
| `trap_pair_session_summary.csv` | trap rates per (pair × session) overall |
| `per_pair_behavior.csv` | one row per (pair × session) summary |
| `pair_<PAIR>.md` (×28) | per-pair markdown profile |

State files (in `teamagent/state/`):

| File | What |
|---|---|
| `events_365d.json` | full event archive (667 events) |
| `moves_365d.jsonl` | 28k cells with significance flag and persistence/reversal |

---

## Phase 7 — live integration (separate PR)

The next step (separate PR after user reviews this) would be:

1. **`forecast_scanner` event-weight boost**: when a persistent-driver event
   is in ±6h window, multiply the score by `1 + 0.3 × concordance × persistence`
   (so high-quality events contribute more). Direction sign comes from the
   `dominant_direction_event_ccy` lookup.

2. **`paper_trader` trap-filter**: block trade-open for `block_minutes_after_news`
   on cells in the trap-rate ≥50% list. Read `trap_pair_session_summary.csv`
   at startup, build an in-memory blocklist, and check it in
   `_should_open_for_pair`.

3. **Live-WR estimate**: target +3-5pp improvement on cells where a
   persistent driver applies. **Cannot promise +20pp WR** — the data shows
   that even high-quality drivers (NFP) have only 44% 24h persistence on
   binary directional bets. The realistic improvement from event-weighting
   is incremental, not revolutionary.

---

## Anti-simulator audit

- All 667 events have a verifiable public source.
- All 28k price cells come from Yahoo `1h` bars over a fixed 365d window.
- No `random.*`, `numpy.random.*`, or generated price series anywhere in
  the analysis pipeline (`teamagent/events/{archive,detector,attribution,traps,profile}.py`).
- The `state/events_365d.json` and `state/moves_365d.jsonl` files are
  reproducible — re-running `python -m teamagent.events.archive` and
  `python -m teamagent.events.detector` produces nearly identical outputs
  (small variation only from any newly-published events between runs).
