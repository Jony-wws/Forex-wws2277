# AGENTS.md — instructions for any AI agent (Devin / Codex / Cursor / etc.)

This file is read automatically by AI assistants when they work in this repo.
Read it BEFORE doing anything else.

> ## 🧠 MANDATORY FIRST READ: [`DEEP_WORK_PROTOCOL.md`](DEEP_WORK_PROTOCOL.md)
> Before strategies, backtests or code — read the Deep Work Protocol.
> It defines HOW to work on ANY task here: autonomous iterative research
> (goal with measurable criterion → hypothesis → test → report → repeat;
> honest validation, no look-ahead bias), NOT chatbot-style single answers.
> **All times in UTC+5 (Tashkent)** — the only working timezone for this project.

## Project: FOREX Сигналы 2026

Real-time forex signal system for **28 currency pairs**.
Created 2026-05-05. This is the only system in this repo.

### Features:
- **Real data only**: Yahoo Finance (live + history). NO simulators.
- **15+ indicators**: RSI, MACD, EMA, Bollinger, Stochastic, ADX, Williams %R, Ichimoku, Momentum, VWAP, Price Action
- **BUY/SELL signals** — only when confidence ≥80%
- **5-hour and 24-hour forecasts** for each pair
- **Order book** — Bid/Ask, spread, market depth, support/resistance
- **Price Action** — candlestick pattern detection
- **UTC+5 timezone**, all UI in Russian
- **Instant loading** — data embedded in HTML (works on mobile Chrome)
- **Auto-refresh** every 10 seconds

## Quick start

```bash
cd ~/repos/Forex-wws2277
pip install -q fastapi uvicorn yfinance pandas numpy
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Dashboard: `http://127.0.0.1:8080/`
First scan takes ~15 sec (downloads data from Yahoo Finance).

## How to start a session for the user

1. Read `git log --oneline -10` and this `AGENTS.md`.
2. Install deps: `pip install -q fastapi uvicorn yfinance pandas numpy`.
3. Start: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8080`.
4. Wait ~15 sec, verify: `curl -s http://127.0.0.1:8080/api/signals | python3 -c "import sys,json; print(len(json.load(sys.stdin)['pairs']),'pairs')"`.
5. Expose: `deploy expose port=8080`.
6. Send URL to user (Android Chrome): `https://user:<password>@<host>/`.

## Layout

```
app/
├── config.py         # 28 pairs, UTC+5, thresholds
├── prices.py         # Yahoo Finance + cache
├── indicators.py     # 13 technical indicators
├── price_action.py   # Candlestick pattern detection
├── orderbook.py      # Order book (Bid/Ask, depth, S/R levels)
├── analyzer.py       # Multi-timeframe analysis + scoring
└── main.py           # FastAPI server + background scanner
static/
└── index.html        # Responsive UI, 2 tabs (Signals + Order Book)
pyproject.toml        # Dependencies
```

## API endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Main dashboard (HTML with embedded data) |
| `GET /api/signals` | All 28 pairs with prices, signals, forecasts |
| `GET /api/orderbook/{pair}` | Order book for one pair |
| `GET /api/orderbooks` | Order books for all pairs |

## Key conventions

1. **Real data only** — Yahoo Finance. Never add simulators or fake data.
2. **Signals only at ≥80% confidence** — don't show signal if not confident.
3. **All UI in Russian** (Русский язык).
4. **UTC+5 timezone** for all times and calculations.
5. **28 currency pairs** — all majors and crosses.
6. **Update every 10 seconds** — both data and UI.
7. **User is on Android Chrome** — optimize for mobile.

## Indicator scoring system

15 voting blocks with total score range ~±30:
- A. 4H Trend (±3)
- B. 1H Confirmation (±2)
- C. 15M Entry (±1)
- D. RSI (±3)
- E. MACD (±3)
- F. Bollinger Bands (±2)
- G. Stochastic (±2)
- H. ADX Trend Strength (±1 to ±3)
- I. Williams %R (±1)
- J. Ichimoku Cloud (±1 to ±3)
- K. Momentum (±1 to ±2)
- L. VWAP (±1)
- M. Multi-TF Agreement (±3)
- N. Price Action (±1 to ±3)
- O. EMA Cross 15M (±1)

Score → Confidence mapping: 0→50%, 5→65%, 8→75%, 10→80%, 15→87%, 20→92%, 25→95%


## Deep Work Protocol — English summary

**Do not behave like a chatbot. Behave like a researcher.**

1. **Goal first**: define a measurable success criterion and hard constraints before working.
2. **Iterate**: hypothesis → test (full backtest, real data) → compare on the PRIMARY metric → next hypothesis. Keep results of every variant.
3. **Interim reports**: regularly show current best numbers (per-month breakdown), what is running now, what is next.
4. **Honesty**: no look-ahead bias, walk-forward validation only, no fitting on the test period, real data only, disclose multiple-testing risk.
5. **Stop only when** the goal is reached honestly OR you can mathematically prove it is unreachable (then say so plainly, with the math).
6. **Timezone**: all times in reports/schedules/signals must be **UTC+5 (Tashkent)**.

This protocol applies to ANY task in this repo (and beyond), not just trading.

---

