# Devin Chat Session Context — 2026-05-02/03

## Summary of conversation
The user (Jony-wws) had a chat session on Devin discussing:

1. **Platform questions**: Asked about using omni route / Claude Code without a PC. Answer: not possible through Devin, but can use GitHub Codespaces, Gitpod, or Google Cloud Shell.

2. **Repository structure**: Confirmed that all code is stored on GitHub. Main code repos are `FOREX21` and `FOREX` (identical copies of Forex Mind AI frontend). The canonical "live" repo is `Forex-wws2277` which contains the full trading system from PR #3.

3. **Devin tunnel URLs are temporary**: URLs like `https://<vm-id>-tunnel-<hash>.devinapps.com/` die when the Devin VM stops. The permanent deployment is on Fly.io: `https://fxinvestment-mjfdsshe.fly.dev/`

4. **Saving context**: User requested that all code from the "Develop fxINVESTMENT on Fly.io" session be preserved. PR #3 contains 80+ commits with the full system. This file was created as part of that preservation effort.

## Key URLs
- Fly.io permanent URL: https://fxinvestment-mjfdsshe.fly.dev/
- Routes: `/` and `/intent` (landing, 28 pairs, charts, BUY/SELL), `/system` (audit panel)

## Repository map
| Repo | Purpose |
|------|---------|
| `Forex-wws2277` | **PRIMARY** — full trading system (bots, agents, dashboard, strategies) |
| `FOREX21` | Forex Mind AI frontend (React/TS) + Python backend |
| `FOREX` | Copy of FOREX21 |
| `Forex-wws27`, `Forex-wws22`, `Forex-wws2` | Empty/backup repos |

## What was built (from PR #3 commits)
- Trading bots v17 through v26 (Deriv, universal, daily, escalating, per-combo, combined)
- Team Agent system: 60 agents + orchestrator + watchdog + dashboard
- Forecast scanner + paper trader (3 parallel strategies)
- Strategy search engine (120 variants × 28 pairs × up to 365 days)
- Macro fundamentals integration (FRED rates/yields/CPI)
- CFTC COT speculator positioning analyzer
- Microstructure PRO module
- Stability engine + premium purple UI
- Market radar (20 scanners) + stakan (volume-profile reversal)
- Deployment to Fly.io
- News blackout (ForexFactory) + DXY filter + S/R levels
- State committer (auto-persist to git every 15 min)

## Trading stats (last recorded)
- trades=5/W2/L3 WR=40% PnL=-1$ (most recent state commit)
- Peak: trades=21/W15/L6 WR=71% PnL=+338$
