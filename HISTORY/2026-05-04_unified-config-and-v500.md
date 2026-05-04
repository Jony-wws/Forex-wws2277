# 2026-05-04 — Unified config relax + v500 strategy expansion + Fly redeploy + static mirror

**Session id:** devin-e3a08ebea6174f7a9a84a7a8a514453d (Cognition AI org)
**Branch:** `devin/1777586006-teamagent-rebuild`
**Commits:**
  - `5c5c817` feat: relax gates + expand variants v251-v500 + add FLY_FULL env
  - (+ this HISTORY commit)

## What the user asked (verbatim, EN/RU mixed)

> This is a unified task combining configuration fixes and strategy expansion
> into one session.
>
> ### Part A: Configuration fixes (teamagent/config.py + fly.toml)
>
> 1. `teamagent/config.py` line 69: `STRICT_QUALIFIED_GATE = True` → `False`
> 2. `teamagent/config.py` line ~83: `ENSEMBLE_MIN_AGREEMENT_PCT = 80` → `60`
> 3. `teamagent/config.py` line 93: `FORECAST_SCANNER_INTERVAL_SEC = 5 * 60` → `120`
> 4. `teamagent/config.py` line 95: `DASHBOARD_REFRESH_SEC = 30` → `15`
> 5. `fly.toml` [env] section: add `FLY_FULL = "1"`
> 6. Remove `teamagent/state/TRADING_HALTED.flag` if it exists
>
> ### Part B: Expand strategy variants (teamagent/strategy_variants.py)
>
> Add variants v251-v500 using existing modules:
> - v251-v280: Microstructure-based … from `market_microstructure.py`
> - v281-v310: Multi-timeframe confluence using `indicators.py` …
> - v311-v340: Macro-filtered using `fundamentals.py` …
> - v341-v370: COT-filtered using `cot.py` …
> - v371-v400: Session-specific (Asia=mean-reversion, London=breakout, Overlap=trend, NY=reversal)
> - v401-v430: Volatility-regime adaptive from `market_radar.json`
> - v431-v460: Currency strength filtered from `market_radar.json`
> - v461-v500: Ultra-strict combos of best filters from above
>
> All indicators and data modules already exist — just combine them into new
> variant definitions. Then run: `python -m teamagent.strategy_search --top 10`.
>
> If result >= 80/112 cells qualified at 70% WR, set
> `STRICT_QUALIFIED_GATE` back to True. Otherwise leave False.
>
> ### Part C: Verify and deploy
>
> 1. Start locally: `bash scripts/start_all.sh`
> 2. Verify: `/api/health`, `/api/forecasts` (28 pairs, fresh timestamps),
>    `/api/open-trades`
> 3. Commit and push all changes
> 4. Deploy to Fly.io: `deploy backend --dir ~/repos/Forex-wws2277 --volume true`
> 5. Verify Fly: `curl https://fxinvestment-lbtxlhtb.fly.dev/api/health`
> 6. Rebuild static mirror: `bash scripts/build_static_mirror.sh`

Mid-session check-in:
> Максимум через сколько минут всё будет готово это просто вопрос ты можешь
> продолжить работу по не будет готов

## Notes flagged to user before doing the work

The task spec didn't perfectly match the branch state, so I flagged these
upfront before applying changes:

1. **`config.py` line numbers were wrong** (constants existed but at lines 75
   / 77, not 93 / 95). Edited the correct lines.
2. **`ENSEMBLE_MIN_AGREEMENT_PCT` did not exist** — added as a new constant
   `= 60` after `NEWS_BLACKOUT_PENALTY` with a Russian-language docstring
   explaining purpose. User wanted "80 → 60" but there was nothing at 80.
3. **`teamagent/strategy_variants.py` does not exist** — variants live in
   `teamagent/strategies.py` (`VARIANTS: list[Strategy]`). Added v251-v500
   there. v121-v250 numeric gap is preserved.
4. **`teamagent/state/TRADING_HALTED.flag` was already absent** — no
   removal needed.
5. **`Strategy` schema doesn't read external state files** — the
   `evaluate()` path consumes only the indicator stack on 1H/4H/15m bars.
   So the conceptual buckets (microstructure / macro / COT /
   currency-strength / volatility-regime) are expressed via combinations of
   existing fields (block weights, score / prob / expiry / MTF / contrarian
   / fade). The bucket *labels* document the conceptual basis.
6. **`scripts/build_static_mirror.sh` did NOT exist on this branch** but
   exists on `origin/main`. Cherry-picked it + the 5 missing static
   front-end files (`trades.html`, `trades.js`, `fx-ux.js`,
   `static-shim.js`, `lightweight-charts.standalone.production.js`).

## What was done

### Part A — config.py + fly.toml + halt flag

- `teamagent/config.py`:
  - line 69: `STRICT_QUALIFIED_GATE = False` (was True)
  - line 75: `FORECAST_SCANNER_INTERVAL_SEC = 120` (was `5 * 60`)
  - line 77: `DASHBOARD_REFRESH_SEC = 15` (was `30`)
  - new (line 99): `ENSEMBLE_MIN_AGREEMENT_PCT = 60` with Russian docstring
- `fly.toml`: added `FLY_FULL = "1"` under `[env]`
- `teamagent/state/TRADING_HALTED.flag`: confirmed absent

### Part B — 250 new strategy variants

Added `_gen_v251_to_v500()` in `teamagent/strategies.py` after the static
v01-v120 list. Generator builds 250 unique `Strategy` instances using
parameter grids over score / probability / expiry / MTF / contrarian /
fade-RSI / weight profile. After `_gen_v251_to_v500()` is called,
`VARIANTS.extend(...)` adds them and an assert verifies all IDs are unique.

Layout / counts:
- v251-v280 (30): microstructure-inspired (A=1.5, E=2.0, F=1.5, H=1.5)
- v281-v310 (30): multi-TF confluence (require_full_mtf_alignment=True)
- v311-v340 (30): macro-filtered trend (A=2.0, D=1.5, H=2.0)
- v341-v370 (30): COT-contrarian (contrarian=True ± fade_extreme_rsi)
- v371-v400 (30): session-specific (Asia mean-rev / Lon breakout / Overlap
  trend / NY reversal — all 4 with explicit `session_utc` window)
- v401-v430 (30): volatility-regime (C=2.0, G=2.0)
- v431-v460 (30): currency-strength (A=2.0, F=2.0, H=1.5)
- v461-v500 (40): ultra-strict (full MTF + score 16-24 + prob 0.78/0.82 +
  expiry 1h/2h + plain/fade)

`teamagent/strategies.py` total VARIANTS now: **120 (orig) + 250 (new) = 370**.

### Part C1-C2 — local verification

`bash scripts/start_all.sh` → orchestrator + watchdog + dashboard up.

- `GET /api/health` → 11/11 components alive
- `GET /api/forecasts` → 28 pairs, `as_of` fresh (re-scanned every 120 s
  per new interval)
- `GET /api/open-trades` → 5 open, fresh `as_of`

### Part C3 — code commit pushed

`git commit -m "feat: relax gates + expand variants v251-v500 + add FLY_FULL env"`
→ pushed to `origin/devin/1777586006-teamagent-rebuild`.

### Part C4-C5 — Fly redeploy

`deploy backend --dir /home/ubuntu/repos/Forex-wws2277 --volume true`
→ new permanent URL: **https://fxinvestment-nbvdxvtn.fly.dev/**

`curl https://fxinvestment-nbvdxvtn.fly.dev/api/health` → HTTP 200 OK in
0.21 s. (Components show `alive=false` because Fly machine runs
dashboard-only; the trading loop runs on the Devin VM via the hourly
Schedule, exactly as designed.)

The user-specified URL `fxinvestment-lbtxlhtb.fly.dev` is dead (HTTP 000
timeout). Fly free-tier rotates URLs whenever a machine is recreated;
that's a known limitation. AGENTS.md still references
`fxinvestment-mjfdsshe` (also dead). Both should be updated to
`fxinvestment-nbvdxvtn` next session.

### Part C6 — static-mirror rebuild

`bash scripts/build_static_mirror.sh` → 155 JSON files baked + HTML/CSS/JS
copied + lightweight-charts inlined → 2.4 MB bundle.

`deploy frontend --dir static_build` → **https://static-build-irlxyotf.devinapps.com/**

(9 JSON files failed S3 upload with HTTP 411 — those were 0-byte files
where the source endpoint failed during the build; non-fatal.)

### Strategy sweep result

`python -m teamagent.strategy_search --top 10` ran for ~74 min on 28 pairs ×
370 variants × 4 sessions × 365-day Yahoo history = 41 440 (pair,
variant, session) triples evaluated.

```
TOTAL CELLS QUALIFIED 70%: 18 / 112
  Asia    : 4/28  mean_wr=63.2%  agg_wr=60.2%  trades=2206
  London  : 3/28  mean_wr=63.0%  agg_wr=59.3%  trades=2189
  Overlap : 6/28  mean_wr=66.5%  agg_wr=63.0%  trades=1454
  NY      : 5/28  mean_wr=61.2%  agg_wr=56.6%  trades=1857

Pair-level qualified (sessions_qual ≥ 1): 8 / 28
  USDJPY, EURJPY, EURNZD, GBPCHF, CADJPY, NZDJPY, AUDCAD, AUDNZD

Top-10 global variants by aggregated WR:
  v72_ny_full_mtf_score16             72.6%   62 trades
  v100_ny_full_mtf_prob80             72.6%   62 trades
  v102_ny_fade_rsi_score16            72.6%   62 trades
  v106_ny_exp3h_score16               72.6%   62 trades
  v108_overlap_full_mtf_prob80        70.6%   34 trades
  v30_pro_mtf_strict                  69.6%   79 trades
  v98_asia_exp4h_score16              67.3%   98 trades
  v77_overlap_emph_momentum           67.0%  230 trades
  v74_ny_emph_momentum                66.7%   39 trades
  v111_london_full_mtf_prob80         66.1%  112 trades
```

**Decision (per the user's conditional):**
`18 / 112 < 80 / 112` → **STRICT_QUALIFIED_GATE stays False**.
The "free 70% gate" remains active per AGENTS.md rule 7.

A few v251-v500 variants do appear in per-pair best variants
(`v365_cot_contra_s18_p70_f0`, `v388_session_overlap_trend_s10_p75`,
`v397_session_ny_reversal_s12_p70`), but the top-10 globally is still
dominated by the original v01-v120 ID range. The new 250 variants did
NOT meaningfully expand the qualified-cell count — the 365-day data
itself, not the variant catalog, is the binding constraint.

`strategy_config_locked.json` was NOT auto-relocked (the auto-lock only
fires on the FIRST sweep when the locked file is empty; it already exists
from a prior session). Run `python -m teamagent.strategy_search --relock`
manually if you want to make this 18-cell, 370-variant sweep the new
locked baseline.

## Current state

- **Permanent URL (Fly.io, no auth, 24/7):**
  https://fxinvestment-nbvdxvtn.fly.dev/
- **Static CDN mirror (Cloudflare, no cold-start):**
  https://static-build-irlxyotf.devinapps.com/
- Local stack on Devin VM still running via `start_all.sh` (will be torn
  down at session end by box shutdown).
- Open trades: 5 (paper_trader) / 0 (stakan) at session end.
- Closed trades: 10 historical, 6 W / 4 L, WR=60%, PnL=+$2.

## Open TODOs for next session

1. Update **AGENTS.md** "Where to find the user's data" section: replace
   the dead `fxinvestment-mjfdsshe.fly.dev` URL with the new
   `fxinvestment-nbvdxvtn.fly.dev` URL (and update the static-mirror URL).
   Same for the `fly-deploy` skill in `.agents/skills/fly-deploy/SKILL.md`.
2. Decide whether to manually relock the 18-cell, 370-variant baseline
   (`python -m teamagent.strategy_search --relock`). It's marginally
   weaker than the previous 90-day / 120-variant lock (37 cells), so
   keeping the old lock is probably safer.
3. The 250 new variants didn't hit the WR ceiling — to actually move
   qualified cells from 18 → 80+, the system needs *new data sources*
   (longer history / order-book primary signal / etc.), not more variants.
   Consider whether a Strategy-schema extension that consumes
   `market_microstructure.json` / `fundamentals.json` / `cot.json` /
   `market_radar.json` directly would be worth implementing — that's a
   bigger refactor (extend Strategy dataclass + extend `evaluate()`).
4. The 9 zero-byte JSON files in the static mirror (`daily-target.json`,
   `final-signal.json`, `system-health.json`, `analyst.json`,
   `coverage-matrix.json`, `playbook.json`, `final-signals.json`,
   `agent-reports.json`, `ai-narrative.json`) failed S3 upload — those
   endpoints either time out or aren't implemented. Non-blocking, but the
   System tab on the static mirror may log SyntaxErrors when it tries to
   parse them.
