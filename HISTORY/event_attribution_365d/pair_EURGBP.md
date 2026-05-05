# EURGBP — 365-day event-attribution profile

_(period: 365 days, Yahoo 1H bars, real events from FRED+CB+COT+geo archive)_

## Asia (00-06 UTC)
- significant moves (>1.5σ): **19** of 252 cells (7.5%)
- traps (significant move reversed ≥80% within 6h): **7** (36.8% of significant moves)

## London (07-12 UTC)
- significant moves (>1.5σ): **17** of 251 cells (6.8%)
- traps (significant move reversed ≥80% within 6h): **0** (0.0% of significant moves)

**Top event drivers:**

| event_type | n | mean_signed_move_pips | direction (event ccy) | concordance | persistence_24h | trap_rate |
|---|---:|---:|---|---:|---:|---:|
| cb_rate_decision | 4 | -25.2 | up | 50% | 46% | 0% |
| cb_press_conference | 1 | -41.1 | down | 100% | 0% | 0% |

## Overlap (13-16 UTC)
- significant moves (>1.5σ): **21** of 251 cells (8.4%)
- traps (significant move reversed ≥80% within 6h): **5** (23.8% of significant moves)

**Top event drivers:**

| event_type | n | mean_signed_move_pips | direction (event ccy) | concordance | persistence_24h | trap_rate |
|---|---:|---:|---|---:|---:|---:|
| cb_rate_decision | 4 | +14.1 | up | 75% | 46% | 75% |
| cb_press_conference | 2 | +21.9 | up | 100% | 58% | 50% |

**Frequent trap setups (≥50% reversal):**

- cb_rate_decision: trap rate **75%** (3/4 significant matches)

## NY (17-21 UTC)
- significant moves (>1.5σ): **21** of 252 cells (8.3%)
- traps (significant move reversed ≥80% within 6h): **10** (47.6% of significant moves)

**Top event drivers:**

| event_type | n | mean_signed_move_pips | direction (event ccy) | concordance | persistence_24h | trap_rate |
|---|---:|---:|---|---:|---:|---:|
| cot_release_neutral | 4 | -23.3 | up | 50% | 0% | 0% |
