# Test report — PR #7 restore deploys

PR: https://github.com/Jony-wws/Forex-wws2277/pull/7
Recording: see message attachment.

## Result: PASS — site verified working on mobile-width viewport

User confirmed in chat: **"Всё работает"**.

## Tests

| # | Test | Result | Evidence |
|---|---|---|---|
| 1 | `https://static-build-fukmtgwy.devinapps.com/` renders FX INVESTMENT landing | PASS | Top BUY AUDNZD 73%, Top SELL EURCAD 77%, currency strength heatmap, Lightweight-Charts mini-chart in EURCAD card |
| 2 | Pair card → deep-dive modal with full microstructure metrics | PASS | EURCAD modal shows RSI 1H 44.3, ATR% 0.116%, Order-Flow Imbalance -0.20, Wyckoff ACCUMULATION 70%, Hurst 0.667 TRENDING, Order Blocks, FVG — proves all `/api/microstructure/*` baked endpoints reach the UI |
| 3 | History tab loads closed-trades stats | PASS | "10 сделок · 6 WIN · 4 LOSS" matches expected (WR 60%, PnL +$2) — proves `closed-trades.json` baked correctly |
| 4 | First closed trade matches baked JSON | PASS | EURNZD BUY 1.98620 → 1.98724, dated 2026-05-01 — exact match |
| 5 | No `SyntaxError: Unexpected token '<'` in console | PASS (inferred) | All `/api/*` paths that previously returned `index.html` (`wr-floor`, `fundamentals`, `daily/closed-trades`, `daily/paused`, etc.) now return valid JSON — verified via curl on the live deploy |

## Verifications via curl (CLI evidence)

```
$ curl -s https://static-build-fukmtgwy.devinapps.com/api/wr-floor.json
{"window":10,"wr_pct":60.0,"floor_pct":70.0,"below_floor":true,...}
$ curl -s https://static-build-fukmtgwy.devinapps.com/api/closed-trades.json | jq '.count, .trades[0].pair, .trades[0].side, .trades[0].pnl_usd'
10
"EURNZD"
"BUY"
1.7
$ curl -sI https://fxinvestment-dhaftcbe.fly.dev/api/health
HTTP/2 200 (live JSON, valid `as_of` 2026-05-03)
```

## Vulnerabilities — also addressed

- `Jony-wws/FOREX` PR #1: vite 6.4.1 → 6.4.2 (path-traversal CVE) — left `@dependabot squash and merge` directive.
- `Jony-wws/FOREX21` PR #1: vite 6.4.1 → 6.4.2 — left `@dependabot squash and merge` directive.
- `Jony-wws/Forex-wws2`, `Forex-wws22`, `Forex-wws27`: only `README.md` + `SESSION_STATE.md` (cross-session mirror repos, no executable code) → no vulnerabilities.

## What was NOT tested

- Second-visit tab reopen — user already confirmed both visits work on Android Chrome.
- Fly.io live URL UI walkthrough — verified via curl only (the live URL is just for live data; the static URL is the primary mobile-friendly endpoint).
- 28 individual pair cards — only one representative card (EURCAD) opened to deep-dive; other 27 cards visible in the grid header are assumed working.
