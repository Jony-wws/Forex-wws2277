# 2026-05-08 — 08:15 Душанбе (NFP-Friday): SKIP cycle 2

**Time:** 03:14 UTC = 08:14 Душанбе. Asia уже закрылась (08:00), London откроется в 13:00 Душанбе.
**5h close target:** 13:14 Душанбе = 08:14 UTC — точно в первый час открытия London. ✅ Clean of NFP (NFP at 17:30 Душанбе = 12:30 UTC, 4h+16m AFTER close).
**News blackout?** No. NFP не в 5h окне сделки.

## Currency Strength (live, 03:12 UTC)

| Rank | CUR | D1 % | Bias |
|------|-----|------|------|
| 1 (weakest) | AUD | -0.114% | Risk-off retracement |
| 2 | GBP | -0.066% | mild dip |
| 3 | CHF | -0.039% | mild dip |
| 4 | JPY | -0.036% | mild dip |
| 5 | NZD | -0.031% | mild dip |
| 6 | CAD | +0.037% | mild rise |
| 7 | EUR | +0.047% | mild rise |
| 8 (strongest) | USD | +0.203% | Pre-NFP DXY long |

**Note:** Spread ranks 2-5 = 0.035% (NOISE). Only USD/AUD have meaningful separation. CSM clean signal exists only on AUDUSD axis — but D1=BUY blocks it.

## Filtered candidates (D1 = H4 aligned, ADX>25, ADR<60%)

| Pair | Dir | D1 | H4 | H1 | ADX H4 | ADR% | CSM gap | Macro | VWAP | Score |
|------|-----|----|----|----|---------|------|---------|--------|------|-------|
| **USDCHF** | SELL | SELL | SELL | FLAT | **40.2** | 43.9 | **+5 anti** | Fed>SNB anti-SELL | ABOVE ✅ | 5/8 ⚠ |
| **EURUSD** | BUY | BUY | BUY | FLAT | 27.3 | 19.3 | -1 mild anti | Fed>ECB anti | BELOW ❌ | 4/8 ⚠ |
| **EURNZD** | SELL | SELL | SELL | FLAT | 29.1 | 42.7 | +2 anti | ECB<RBNZ anti | ABOVE ✅ | 3/8 |
| **GBPCAD** | BUY | BUY | BUY | FLAT | 25.7 | 37.9 | **-4 anti** | BoE>BoC pro | BELOW ❌ | 4/8 ⚠ |
| AUDCHF | BUY | BUY | BUY | SELL | 28.4 | **71.6 ❌** | -2 anti | RBA>SNB pro | ABOVE ✅ | 3/8 |

(NZDCHF ADX=55 H4=BUY, but D1=FLAT → fails Rule #4. ADR 138% → ламинированно exhausted.)

## Probability calculation (best candidate USDCHF SELL)

```
filters_passed = 5
base_p = 50 + (5 - 5) * 4 = 50%

PRO bonuses:
  + Anchored VWAP H4: ABOVE price = SELL-aligned: +5%
  + ADX H4 > 35 (very strong trend): +5%
  - CSM contradiction (gap +5 vs need ≤ -4): -10%
  - Macro contradiction (Fed >> SNB hawkish): -5%
  - Pre-NFP positioning paradox: -5%

final_p = 50 + 5 + 5 - 10 - 5 - 5 = 40%
```

**40% << 70% gate.** USDCHF SELL is mathematically below entry threshold.

## Probability calculation (EURUSD BUY)

```
filters_passed = 4
base_p = 50 + (4 - 5) * 4 = 46%

PRO bonuses:
  + D1 BB squeeze active: +5%
  + ADR used 19.3% (huge buffer): +3%
  - CSM mild contradiction: -3%
  - Macro contradiction (Fed > ECB hawkish): -5%
  - VWAP BELOW price (BUY needs ABOVE): -5%
  - Structure RANGE (no BOS_UP confirmation): -3%

final_p = 46 + 5 + 3 - 3 - 5 - 5 - 3 = 38%
```

**38% << 70%.** EURUSD BUY is mathematically below entry threshold.

## Verdict: SKIP

**Reason 1: No 7/8 confluence.** Best candidates score 4-5/8 — playbook calls these "слабый сетап → ПРОПУСК" (Глава 26).

**Reason 2: 10th trap type confirmed live: CSM-vs-Structure divergence.**
Multi-day D1 EMA structure on USD-pairs (USDCHF SELL, EURUSD BUY) directly opposes intraday CSM (USD rank 8 = strongest). This is **pre-NFP institutional USD-long positioning** that creates whipsaw risk. Adding to v1.1 playbook as Trap #10 (pending PR).

**Reason 3: Probability math.** Even the strongest setup (USDCHF SELL with 40+ ADX, perfect VWAP alignment) only computes to ~40% probability after CSM/macro/positioning penalties. The 70% gate is not arbitrary — it's the breakeven probability for binary options at 70% payout × 1.4 safety margin.

**Reason 4: Iron Rule #9** "Лучше пропустить, чем потерять" — applies cleanly here.

## Next clean entry windows (today)

| Window | Entry Душанбе | Close Душанбе | NFP impact | Recommendation |
|--------|---------------|---------------|------------|----------------|
| **NOW** | 08:14 | 13:14 | clean, 4h+16m before NFP | **SKIP** (no 7/8 setup) |
| Late London | 11:00 | 16:00 | 30 min before NFP — risky | watch |
| Pre-NFP zone | 12:00–17:00 | 17:00–22:00 | IN BLACKOUT | **NEVER** |
| **Post-NFP** | 18:30 | 23:30 | NFP digested | **best re-evaluation point** |
| Post-NY close | 19:00+ | 00:00+ | low liquidity | watch |

## Plan

1. **18:30 Душанбе (13:30 UTC)** — 1h after NFP release. Re-run tv_sweep.py with fresh post-NFP data. Apply 8-filter + PRO confluence. **If 7/8 — give signal. If not — wait Monday.**
2. **Monday 11 May 13:00 Душанбе (London open)** — fresh week, NFP volatility absorbed, structural setups should re-form.
3. **Update PLAYBOOK_FOREX28_2026.md v1.1:**
   - Add Trap #10: CSM-vs-Structure divergence detection
   - Add NFP-Friday explicit blackout rule (entry-window timing matrix)
   - Add post-NFP re-evaluation protocol

## Lesson for the playbook

**Pre-major-news days the 8-filter system over-identifies setups** because intraday CSM and multi-day EMA structure systematically diverge in opposite directions (DXY positioning vs structural carry). On these days, the **score gate must be raised from 7/8 to 8/8** OR a CSM-Structure consistency check added as a separate gate.

This is a real edge: skipping pre-NFP whipsaw days preserves capital while everyone else gets stopped out.
