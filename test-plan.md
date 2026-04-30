# Test Plan — TeamAgent FOREX AI rebuild (PR #1)

**Live URL:** https://aaa722b1109e-tunnel-8apkrjro.devinapps.com/
**Basic Auth:** `user` / `6d20d672089e9b3c96ae7ef57e8c05fd`

## What changed (in user-visible terms)
The previous session's work was lost; the dashboard had two conflicting tables (meta-voting vs PROGNOZY-28), no live open-trade panel, and Volume Profile gave no forecast. PR #1 rebuilds the system with:
1. ONE forecasts table (PROGNOZY-28) — no separate meta-voting
2. Open-trade panel that ticks every 30 sec with entry price, time, expiry countdown, current price, projected PnL
3. Volume Profile (Стакан) that returns a forecast `couldn't return to ... until 00:00 UTC+5`
4. Real data only (Yahoo Finance + Dukascopy + ForexFactory) — no simulators

## Primary E2E flow

### Test 1 — Single source of truth (paper-trader uses same forecast as PROGNOZY-28)
**Steps**: Open dashboard → record top entry of "Сейчас открыто" (e.g. EURCAD SELL 1.59318) → record top entry of "PROGNOZY-28" (e.g. EURCAD SELL 81.1%).
**Pass criteria**: The pair+side of the highest-prob row in PROGNOZY-28 matches the same pair+side of an open trade. If PROGNOZY says EURCAD SELL but an open trade says EURCAD BUY, this is a FAIL — would expose conflicting sources.
**Adversarial check**: A broken implementation with separate meta-voting would show e.g. PROGNOZY EURCAD SELL but open trade EURCAD BUY (since paper-trader read a different source). They MUST match.

### Test 2 — Live open-trade panel updates every 30 sec
**Steps**: Note `Осталось до экспирации` of trade #1 (e.g. `2ч 56м 07с`) and current_price → wait 35 sec → re-take screenshot → compare.
**Pass criteria**:
- countdown DECREASED by ~30 sec (not unchanged)
- "Обновление" timestamp top-right advanced by ~30 sec
- current_price field exists and is a valid 5-decimal number
**Adversarial check**: A static page that doesn't refresh would show identical countdown 35 seconds later. The countdown MUST move.

### Test 3 — PROGNOZY-28 detail click reveals breakdown (one-source-of-truth proof)
**Steps**: Click any row in PROGNOZY-28 (e.g. EURCAD).
**Pass criteria**: Detail panel appears below the table showing:
- pair + side + probability_pct + score (e.g. `EURCAD SELL 81.1% score -16/44`)
- Two columns: «За (N)» and «Против (N)» with named agent rules (e.g. `4H_strong_downtrend, 1H_downtrend, MTF_full_bear`)
- The number of "За" agents + "Против" agents > 0 (i.e. agents-for/against integrated, not external)
**Adversarial check**: If meta-voting were a separate table, the breakdown would say "no agents data" or zero on both sides. There MUST be ≥1 agent on at least one side.

### Test 4 — Volume Profile (Стакан) returns a forecast
**Steps**: Scroll to "Стакан (Volume Profile) с прогнозом до 00:00 UTC+5" → select EURCAD → observe.
**Pass criteria**:
- Header shows POC, VAH, VAL with numeric prices (e.g. `POC 1.59586 · VAH 1.59898 · VAL 1.59291`)
- Direction shown as UP or DOWN
- Section "Куда не вернётся:" lists ≥1 level with format `↓/↑ ниже/выше PRICE — вес X.X% (поддержка/сопротивление)`
- Histogram has ≥1 row marked `🐋 кит` (big-player)
**Adversarial check**: If the Stakan returned no forecast (old behavior), the "Куда не вернётся" list would be empty and there would be no big-player markers. Both MUST be present.

### Test 5 — All 4 components and 60 agents are alive
**Steps**: Look at header strip and "60 агентов" panel.
**Pass criteria**:
- Header shows 4 green dots: forecast_scanner, paper_trader, orchestrator, watchdog
- 60-agent grid: count of agents with green left border ≥ 58 (allow 2-second startup slack)
- Each agent badge shows age `<120s` (heartbeat fresh)
**Adversarial check**: If watchdog/orchestrator weren't running, header would show red. If agents were dead, their borders would be red. We MUST see green.

## What is NOT tested here (out of scope for this run)
- Settlement of an open trade (requires waiting 1-4 hours; covered by future regression observation)
- LLM agents producing real reasoning (no API keys yet)
- Auto-restart correctness (would need to kill an agent and wait 60+ sec)

## Risk
- All 5 tests run on the SAME live deployment behind Basic Auth.
- Tunnel URL is ephemeral; if VM restarts, the URL dies.
- A flaky Yahoo Finance call could make a forecast row appear with `None` data — this would be a real-data resilience issue, not a test failure.
