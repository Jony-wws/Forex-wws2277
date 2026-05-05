# USDJPY — 365-day event-attribution profile

_(period: 365 days, Yahoo 1H bars, real events from FRED+CB+COT+geo archive)_

## Asia (00-06 UTC)
- significant moves (>1.5σ): **24** of 250 cells (9.6%)
- traps (significant move reversed ≥80% within 6h): **5** (20.8% of significant moves)

**Top event drivers:**

| event_type | n | mean_signed_move_pips | direction (event ccy) | concordance | persistence_24h | trap_rate |
|---|---:|---:|---|---:|---:|---:|
| cb_rate_decision | 3 | +31.0 | down | 67% | 74% | 33% |
| jp_unemp | 1 | -79.0 | up | 100% | 100% | 0% |

## London (07-12 UTC)
- significant moves (>1.5σ): **25** of 250 cells (10.0%)
- traps (significant move reversed ≥80% within 6h): **5** (20.0% of significant moves)

**Top event drivers:**

| event_type | n | mean_signed_move_pips | direction (event ccy) | concordance | persistence_24h | trap_rate |
|---|---:|---:|---|---:|---:|---:|
| us_claims | 4 | -7.0 | up | 75% | 47% | 0% |
| us_nfp | 3 | -76.6 | down | 67% | 88% | 0% |
| us_unrate | 3 | -76.6 | down | 67% | 88% | 0% |
| us_pce | 2 | -123.6 | up | 50% | 81% | 0% |
| us_core_pce | 2 | -123.6 | up | 50% | 81% | 0% |

## Overlap (13-16 UTC)
- significant moves (>1.5σ): **19** of 249 cells (7.6%)
- traps (significant move reversed ≥80% within 6h): **1** (5.3% of significant moves)

**Top event drivers:**

| event_type | n | mean_signed_move_pips | direction (event ccy) | concordance | persistence_24h | trap_rate |
|---|---:|---:|---|---:|---:|---:|
| us_claims | 6 | -27.8 | down | 67% | 33% | 0% |
| us_retail | 1 | +100.8 | up | 100% | 21% | 0% |
| us_refunding | 1 | +103.5 | up | 100% | 4% | 0% |
| us_cpi | 1 | -101.8 | down | 100% | 0% | 0% |
| us_core_cpi | 1 | -101.8 | down | 100% | 0% | 0% |
| us_ppi | 1 | -101.8 | down | 100% | 0% | 0% |
| us_pce | 1 | -23.7 | down | 100% | 38% | 0% |
| us_core_pce | 1 | -23.7 | down | 100% | 38% | 0% |

## NY (17-21 UTC)
- significant moves (>1.5σ): **18** of 252 cells (7.1%)
- traps (significant move reversed ≥80% within 6h): **10** (55.6% of significant moves)

**Top event drivers:**

| event_type | n | mean_signed_move_pips | direction (event ccy) | concordance | persistence_24h | trap_rate |
|---|---:|---:|---|---:|---:|---:|
| cb_rate_decision | 5 | +34.0 | up | 80% | 80% | 60% |
| cb_press_conference | 5 | +34.0 | up | 80% | 80% | 60% |
| cot_release_neutral | 1 | -44.1 | up | 100% | n/a | 0% |
| geo_inauguration | 1 | +24.8 | up | 100% | 46% | 100% |
| cot_extreme_short | 1 | -184.6 | up | 100% | 0% | 0% |

**Frequent trap setups (≥50% reversal):**

- cb_rate_decision: trap rate **60%** (3/5 significant matches)
- cb_press_conference: trap rate **60%** (3/5 significant matches)
