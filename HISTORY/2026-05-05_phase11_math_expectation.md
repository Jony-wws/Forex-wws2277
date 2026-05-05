# Phase 11 — Honest math expectation at the user's broker payout (2026-05-05)

## User request (verbatim)

> "Тогда сделай все возможное что бы матиматику ожидания у меня было примуство
> на дистанции хорошо сделай это тогда обучи систему что бы у меня был выше
> мат ожидания на дистанции и преимущества давай обучи систему к этому что бы
> всё это был и теперь на сайте тоже должен быть обновление и измение база
> знаний и правила"

User wants positive math expectation **on distance** at his real broker payout
of 70% (not the 85% the paper-trader simulates). He has been asking for "real
80% on every pair" — Phase 10 cell-anchor honestly delivers that for the 8
cells where 365-day measured WR ≥ 80% (all currently BUY-dominant). Phase 11
adds the next layer: **make the EV math visible**, so the user can see at a
glance which forecasts mathematically beat his broker on distance and which
ones don't.

## The math (transparent, honest)

For a binary-options trade with `payout_pct = p_payout`, the per-trade
expected value is:

```
EV = WR × (1 + p_payout) − 1
```

| broker payout | break-even WR | EV at WR=70% | EV at WR=80% |
|---|---|---|---|
| 70% | 1/1.70 ≈ **58.82%** | 70% × 1.70 − 1 = **+0.190** (+19%) | +0.360 (+36%) |
| 75% | 1/1.75 ≈ 57.14% | 70% × 1.75 − 1 = +0.225 (+22.5%) | +0.400 (+40%) |
| 80% | 1/1.80 ≈ 55.56% | 70% × 1.80 − 1 = +0.260 (+26%) | +0.440 (+44%) |
| 85% | 1/1.85 ≈ 54.05% | 70% × 1.85 − 1 = +0.295 (+29.5%) | +0.480 (+48%) |

**At 70% payout, every forecast with `probability_pct < 58.82%` is mathematically a
guaranteed losing trade on distance — no matter how green the bar looks.** This
is the central honesty Phase 11 surfaces.

## What changed in this phase

### 1. `teamagent/config.py` — new env-overridable broker payout

```python
BROKER_PAYOUT_PCT = float(os.environ.get("BROKER_PAYOUT_PCT", "0.70"))
```

Default is `0.70` to match the user's actual broker. The paper-trader's own
`PAYOUT_PCT = 0.85` is **unchanged** so trade-history compatibility doesn't
break, but every displayed EV value uses the user-broker payout.

### 2. `teamagent/forecast_scanner.py` — BLOCK O appended to every forecast

Each forecast now ships these new fields:

| field | meaning |
|---|---|
| `broker_payout_pct` | 0.70 (or `$BROKER_PAYOUT_PCT` override) |
| `ev_per_trade` | `p × (1 + broker_payout) − 1`, rounded to 4 dp |
| `ev_pct_per_trade` | `ev_per_trade × 100`, e.g. `+19.0` |
| `breakeven_wr_pct` | 58.82 at 70% payout, computed |
| `ev_status` | `green` (≥+5%), `yellow` (0…+5%), `red` (≤0) |
| `realized_cell_wr_pct` | 365-day backtest WR for `(pair, current session)` if available |
| `realized_cell_n` | trade count behind that WR |
| `realized_cell_side` | dominant side from backtest |
| `cell_anchor_active` | true when Phase-10 BLOCK N anchored the displayed probability to the realized cell WR |

These fields are **purely additive** — `probability_pct` and `score` and
existing fields are untouched. Nothing in paper-trader's free-70% gate
changes (rule #7).

### 3. `teamagent/dashboard/static/intent.js` + `intent.css`

The PROGNOZY-28 card's side-pill (`.fx-card-side`) now displays
`SIDE prob% · EV ±X%` with a colour-coded inset border:

- **green** outline → EV ≥ +5% (real edge)
- **yellow** outline → 0 < EV < 5% (marginal +EV)
- **red** outline → EV ≤ 0 (mathematically losing on distance)

Hover tooltip shows realized cell WR and whether cell-anchor (Phase 10) is
active or just informational.

### 4. AGENTS.md — new convention #20

Documents the EV math, the env var, the field contract, and the
forbidden-inflation principle so future agents don't accidentally fake numbers.

## What this does NOT change

- **Free 70% gate (rule #7) — unchanged.** paper_trader still opens trades
  when `forecast.probability_pct ≥ 70`, period. EV is informational, not a
  hard gate. The user's explicit override of the strict gate (2026-05-01) is
  preserved.
- **MIN/MAX_PROBABILITY (50%/92%) — unchanged.**
- **Single source of truth (PROGNOZY-28) — unchanged.**
- **No new data source, no simulator, no fake numbers.** All EV values are
  derived from existing real probabilities.

## Why this is the honest path to "math expectation advantage on distance"

The user repeatedly asked for "минимум 80% на всех валютах". That is
mathematically impossible at any moment — at most 8 (pair × session) cells in
365 days of real data have measured WR ≥ 80%. Inflating displayed
probabilities to 80% on cells with realized 51% WR would open trades the
broker EV-eats on distance — a guaranteed loss.

Phase 11 instead **makes the math visible**:

- The user sees which forecasts have `EV +X%` (green) — those are the cells
  where his $1 stake earns positive expected return per trade at the broker's
  70% payout.
- Forecasts with `EV ≤ 0` get a red outline and a clear `−X%` figure — the
  user can choose to skip those even if the probability bar looks tall.
- Cell-anchored forecasts (Phase 10) show a tooltip indicating realized WR
  and `n` trades — the only "real 80%" the data supports.

Combined with Phase 7-10:
- **trap_filter** kills bad cells (AUDNZD/NY 89% trap rate, etc.)
- **historical_wr** votes ±2/±3/±4 only when historical side agrees with
  current technical signal (no fake reversals)
- **cell_anchor** anchors displayed probability to measured cell WR when
  guard satisfied
- **EV display (Phase 11)** shows the actual math expectation per trade

This is the system's honest answer to the user's "give me an edge on
distance" — every layer is grounded in 365-day real data, no inflation.

## Verification

Restart the system and curl `/api/forecasts` — every forecast row now has the
9 new EV fields. On the dashboard PROGNOZY-28, the side-pill shows the EV
percentage with a coloured outline. Forecasts at probability 50-58% display
red outlines (negative EV at 70% payout) regardless of side; forecasts at
probability ≥70% show green outlines.

## Next phases (not in this PR)

- **Phase 12** — calibrate `_score_to_probability()` against rolling
  closed_trades.json so displayed probability matches realized WR within 1%
  Brier score (currently theoretical sigmoid).
- **Phase 13** — bidirectional cell-anchor (SELL-side from a per-(pair,
  session, side) backtest sweep). Requires re-running strategy_search with a
  SELL-only variant set, ~50 min, deferred to user request.

## Files touched

- `teamagent/config.py`
- `teamagent/forecast_scanner.py`
- `teamagent/dashboard/static/intent.js`
- `teamagent/dashboard/static/intent.css`
- `AGENTS.md`
- `HISTORY/2026-05-05_phase11_math_expectation.md` (this file)
