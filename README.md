# Forex-wws2277 — FOREX AI 2026 (TeamAgent)

Multi-agent paper-trading system for 28 forex pairs. Real data only
(Yahoo Finance + Dukascopy + ForexFactory). Real win-rate gate. Live dashboard.

> **For AI agents (Devin / Codex / Cursor / Claude Code):**
> Read [`AGENTS.md`](./AGENTS.md) first. It explains everything you need to
> continue work without re-asking the user.

## Quick start

```bash
pip install -q -r teamagent/requirements.txt
bash scripts/start_all.sh
# dashboard at http://127.0.0.1:8080/
```

Stop: `bash scripts/stop_all.sh`.

## What is this?

- **forecast_scanner** scans 28 currency pairs every 5 min using multi-TF
  technical analysis (4H + 1H + 15m, EDGE-44 score, Bollinger / RSI / VWAP
  / Volume Profile, etc.) and produces unified forecasts (PROGNOZY-28).
- **backtester** walks the same logic over the last 30 days of real Yahoo data
  to compute the **REAL** win-rate per pair. This is the truth.
- **paper_trader** opens virtual $50 / 85% binary trades (1-4h expiry) ONLY
  when both `probability ≥ 70%` and `backtest_30d[pair].win_rate ≥ 70%`.
  This is the "real 70% WR" gate.
- **strategy_search** (run on demand) iterates over 30+ strategy variants
  (scoring weights, expiry windows, session filters, news filters,
  mean-reversion vs trend-following) to find the configuration that maximizes
  real WR per pair × per session.
- **dashboard** serves a live FastAPI/JS UI: open trades with live PnL every
  30 sec, PROGNOZY-28 with backtest WR per pair, Volume Profile with
  "where price will not return to before midnight UTC+5".
- **orchestrator + watchdog** keep all subprocesses alive with heartbeat
  checks and auto-restart.
- **state_committer** auto-commits and pushes `state/*.json` to git every
  15 min so trade history survives across Devin sessions.

## Repo layout

```
teamagent/                  Python package
├── config.py               28 pairs, sessions, thresholds
├── data/                   yahoo.py, dukascopy.py, news.py
├── indicators.py           RSI/EMA/ATR/BB/Momentum/CEI/OFI/VWAP/BBP
├── volume_profile.py       POC/VAH/VAL + forecast to 00:00 UTC+5
├── forecast_scanner.py     5-min loop, the PROGNOZY-28 source
├── paper_trader.py         $50/85% trades, gated on real WR
├── backtester.py           hourly 30-day walk-forward backtest per pair
├── strategy_search.py      finds best config per pair × session
├── orchestrator.py         spawns 60+ child processes
├── watchdog.py             heartbeat health checks
├── state_committer.py      auto-commits state/*.json every 15 min
├── dashboard/              FastAPI + vanilla JS UI
├── agents/                 60 specialised subprocess agents
└── state/                  forecasts.json / open_trades.json / etc.

scripts/
├── start_all.sh            launch orchestrator + watchdog + dashboard
└── stop_all.sh             clean shutdown

infra/
└── fly/                    Dockerfile + fly.toml for Fly.io deploy
                            (permanent URL, survives Devin VM shutdown)
```

## Environment variables (optional)

| Var | Purpose |
|---|---|
| `GROQ_API_KEY` | enables LLM news-sentiment agent (no-op without) |
| `GOOGLE_API_KEY` | second LLM agent |
| `OPENROUTER_API_KEY` | third LLM agent |
| `DERIV_DEMO_TOKEN` | optional Deriv quotes alongside Yahoo |

## Live URLs

- **Devin tunnel** (dies when session ends):
  `https://<session-tunnel>.devinapps.com/`
- **Permanent (fly.io)**: see latest commit body or `infra/fly/README.md`
- **GitHub**: https://github.com/Jony-wws/Forex-wws2277

## Hourly automation

A Devin Schedule named **"FOREX AI hourly run"** (id
`sched-083b11171a0841668f4608b075d769b5`) launches a short session every
hour: pulls latest, starts the system for ~10 min so scanner / paper_trader /
backtester / state_committer make a pass, then exits cleanly. State is
committed and pushed automatically.

## License

Private (currently no license).
