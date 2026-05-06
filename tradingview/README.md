# TradingView Pine Script — FOREX-28 v2 (binary options)

Three scripts for TradingView, all mirror the logic of the Python backend
(`app/analyzer.py`).  v2 is **binary-options first** — no TP / no SL — and
designed to be used from the TradingView Android app (no PC needed).

| File | Purpose |
|---|---|
| **`eurusd_70wr_strategy.pine`** ⭐ | **Single-file EUR/USD-only** trend-pullback strategy targeting honest **≥70% Win Rate** on M15 with 5h binary expiry.  Acts as **both indicator and strategy in one file** — large BUY/SELL labels on chart + official Strategy Tester results + on-chart stats panel.  Uses Supertrend + Hull MA + multi-TF EMA200 + ADX + VWAP (no RSI/MACD as primary signal).  Hard EUR/USD pair-guard. |
| **`eurusd_mr_pro.pine`** | **Mean-reversion variant** for EUR/USD M15 5h binary.  Uses BB + RSI + Stoch confluence at extremes, with multi-TF + session + ATR + ADX + Hull-slope filters.  Has two modes: **`WR_MAX`** (strict, target ~70% WR, ~1-2 trades/day) and **`FREQ_MAX`** (looser, target 5+ trades/day, ~62-66% WR).  Use this when the trend-pullback variant gives too few signals. |
| `forex_signals_indicator.pine` | **Indicator** (FOREX-28 universal) — BUY / SELL / REVERSE labels on chart **plus** an on-chart live backtest panel (Win Rate, Profit Factor, Net P&L, Daily DD).  Self-contained: tracks every signal it ever fired and updates stats in real time as each trade hits its expiry. Run this on the chart you actively watch. |
| `forex_signals_strategy.pine`  | **Strategy** (FOREX-28 universal) for TradingView's Strategy Tester. Same scoring and entry logic as the indicator, wired into the Strategy Tester so you get the official Win Rate / Profit Factor / Max Drawdown reports across multi-year history. Use this for honest per-pair vetting (build your allowlist from it). |

## TL;DR — for EUR/USD only, 5h binary, M15

Use **`eurusd_70wr_strategy.pine`** — it's a single file that does everything.
Open EUR/USD on M15, paste the file in Pine Editor → Save → Add to chart.
BUY/SELL labels appear on chart, Strategy Tester gives the WR/PF/Net stats.
Scroll left to load more history → trade count grows past 500.

## Trading model (binary options)

* Enter **BUY**  on a fresh `score > 0`, `confidence ≥ gateConf`.
* Enter **SELL** on a fresh `score < 0`, `confidence ≥ gateConf`.
* Hold until `expiryH` hours have elapsed → close at market (**no TP / no SL**).
* Win = price closed in the predicted direction at expiry, otherwise loss.
* **Reverse**: if a strong opposite signal fires before expiry, close the
  current position at market and open the opposite.  Marked on chart with a
  bigger `↻BUY` / `↻SELL` label.

## Scoring (15 voting blocks — same as `app/analyzer.py`)

* A0. **D1 trend** (±3) — EMA50/EMA200 alignment.
* A.  **H4 trend** (±3) — EMA50/EMA200 alignment.
* B.  **H1 confirmation** (±2) — EMA20/EMA50 alignment.
* C.  **M15 entry** (±1) — close vs EMA20.
* D.  **RSI(14) on H1** (±3) — overbought / oversold zones.
* E.  **MACD(12,26,9) on H1** (±3) — histogram cross + slope.
* F.  **Bollinger %B on H1** (±2) — position in the channel.
* G.  **Stochastic(14) on H1** (±2) — extremes + crossover.
* H.  **ADX(14) on H1** (±3) — trend strength × +DI/-DI direction.
* I.  **Williams %R on H1** (±1) — extremes.
* J.  **Ichimoku on H1** (±3) — cloud position + Tenkan/Kijun.
* K.  **Momentum 10-bar on H1** (±2) — % change.
* L.  **VWAP on H1** (±1) — when volume present.
* M.  **Multi-TF agreement** (±3) — D1+H4+H1+M15 unanimity.
* N.  **Price Action on H1** (±2..±3) — engulfings + hammers + shooting stars.
* O.  **EMA20/50 cross on M15** (±1) — early entry confirmation.

Score → Confidence: `confidence = 50 + 45 · (1 − e^(−3.66 · score/max))`,
clipped to 50–95%.

## On-chart live backtest panel (indicator)

The indicator simulates every signal it ever fired, evaluates each trade at
its expiry, and continuously updates these numbers right on the chart:

* **Win Rate** — green if ≥70%, orange if ≥56% (binary break-even at 80%
  payout), red below.
* **Profit Factor** — assuming an 80% binary payout (configurable in code).
* **Total signals** + **open positions** count.
* **Net P&L** in payout units (positive = winning).
* **Equity %** + delta from start.
* **Today** — WR / wins / losses today.
* **Day DD** vs the configured limit (`dailyDDLimit%`, default 50).
* **Horizon** in hours.
* **Pair status** — `✅ active` or `🔇 muted (not in allowlist)`.

## Per-pair allowlist

`allowlist_inp` (an input string, comma-separated) controls which symbols the
indicator fires on.  By default it permits the major OANDA / FX / FX_IDC
EUR/GBP/USD/JPY/AUD/CAD/NZD/CHF combinations.  To curate:

1. Run `forex_signals_strategy.pine` in **Strategy Tester** for every pair you
   care about, on the timeframe you trade (M15 / M30 / H1 / H4).
2. Note the Win Rate.
3. Add only the pairs that pass your `minPairWR` threshold (default 70%) to
   the allowlist string.  Removed pairs show `🔇 muted` and won't fire.

## Daily DD halt

If today's drawdown crosses `dailyDDLimit%` (default 50%) the indicator stops
firing **new** signals until the next UTC day rolls over.  Open virtual
trades still resolve at their expiries.  Status is shown in the on-chart
table (red `🛑 DD HALT` banner).

## How to use in the TradingView Android app

1. Open TradingView → choose a pair (e.g. `OANDA:EURUSD`).
2. Bottom toolbar → **Pine Editor** → paste the contents of
   `forex_signals_indicator.pine` → **Save** → name it (e.g. `FX-28 v2`).
3. **Add to chart**.
4. The on-chart panel appears top-right (you can move it via the *Позиция
   таблицы* input).  BUY / SELL / `↻` labels appear at signal bars.
5. To verify the backtest officially: replace step 2 with
   `forex_signals_strategy.pine` and check the **Strategy Tester** tab.

Defaults are tuned for binary options: `gateConf=80`, `expiryH=5`,
`allowReverse=true`, `dailyDDLimit=50%`.  Recommended timeframe: **M30** or
**H1**; works on **M15 / M30 / H1 / H4**.

## How to iterate

* Want **more signals** → lower `gateConf` to 75 (more trades, lower WR).
* Want **higher WR** → raise `gateConf` to 85 / 90 (fewer trades, higher WR).
  But on most pairs no setting will push WR above ~65% honestly — this is the
  reality of technical-indicator-only systems on FX.
* Want **longer trades** → raise `expiryH` to 12–24 (better for swing).
* Want **only the best pairs** → run the strategy file per pair, drop the
  losers from `allowlist_inp`.

You can change every parameter in the indicator's settings dialog without
touching the code.
