# Test report — PR #5 WR-maximization (2026-05-03)

PR: https://github.com/Jony-wws/Forex-wws2277/pull/5
Permanent dashboard: https://fxinvestment-vsxcxrqj.fly.dev/
Local dashboard during test: http://127.0.0.1:8080/
Recording: 4 minutes, attached.

## How I tested

Started the full system on the VM with `bash scripts/start_all.sh` (orchestrator + paper_trader + dashboard + 64 sub-agents on :8080). Walked through both dashboard routes, then opened a terminal next to the browser and ran 7 falsifiable assertions covering all 9 PR-#5 deliverables.

## Result summary

All 7 tests passed. No deviations from the test plan.

| # | Test | Result |
|---|---|---|
| 1 | Cinematic dashboard renders 28 pairs with probability badges and indicator panels | passed |
| 2 | `/system` audit dashboard shows 15/15 self-check green | passed |
| 3 | `len(VARIANTS) == 250` (was 120 on `main`) | passed |
| 4 | `qualified=30/112` cells in `strategy_config.json`, all with 10 `top_variants` | passed |
| 5 | `_exceeds_correlation_limit`: 3rd EUR-pair → `(True, 'EUR')`, GBPUSD → `(False, None)`, 8 currency blocks | passed |
| 6 | `_ensemble_decide` on 4-BUY/1-SELL real variant ids → `side=BUY n_agree=4/5 required=4` | passed |
| 7 | Full unit test suite — `138 passed in 3.48s` | passed |

## Notes / honesty section

- I do **not** have a frame in the recording showing `gate_mode=ensemble` or "корреляционный лимит" actually firing in the live `paper_trader.out` log, because Forex market is currently closed (Sunday 01:40 UTC; market re-opens Sun 22:00 UTC). The log line `paper: MARKET CLOSED — пропускаем открытие новых сделок` is what's printed every second instead, and that's correct, market-aware behavior — but it means the new gates aren't reachable until the weekly market re-opens. That's why I demonstrated the new gates by directly invoking `_exceeds_correlation_limit` and `_ensemble_decide` with real variant ids in a Python REPL — same code path that the paper_trader will execute next Sunday at 22:00 UTC.
- I had to retry TEST 4 once. The first attempt used fake variant ids (`v01`, `v02` …) that don't exist in the catalog, so `_ensemble_decide` correctly returned `None` (no eligible variants). Retrying with real catalog ids (`v01_baseline`, `v02_score12`, `v03_score16`, `v04_prob75`, `v05_prob80`) produced the expected `side=BUY n_agree=4/5`. This is a test-setup nit, not a code defect — the original behavior on unknown ids (return None) is the correct conservative stance.
- The "Главный стратегический агент" section on `/system` shows **14** quality cells — that's a separate agent (`strategy_meta_agent`) that does its own 4 × 120 sweep on a different schedule. The PR #5 result is the **30/112** count that comes out of `strategy_config.json` (the bigger 250-variant 365-day sweep). Both numbers are real and live in their own state files.

## Where the proof lives

- Code:
  - 250 variants — `teamagent/strategies.py` lines 700-1218 (new families v121-v250)
  - Ensemble — `teamagent/paper_trader.py` `_ensemble_decide()` at line 218
  - Correlation filter — `teamagent/paper_trader.py` `CURRENCY_BLOCKS` at line ~50, `_exceeds_correlation_limit()` at line ~98
  - 6 indicators — `teamagent/indicators.py` `macd`, `stochastic`, `adx_indicator`, `williams_r`, `ichimoku`
- State (committed to git in PR #5):
  - 30 qualified cells — `teamagent/state/strategy_config.json`
  - locked baseline — `teamagent/state/strategy_config_locked.json`
- History: `HISTORY/2026-05-03_wr_maximization.md` (full result table)
