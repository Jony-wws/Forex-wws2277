# 2026-05-02 — Static CDN mirror on devinapps.com (mobile reachability fix)

## What the user reported (verbatim, RU)

> "Сайт не открывается у меня вообще что присходит вообще у тебя ес видео работает у меня на телефоне Chrome не открывается даже вообще даже с vpn"

User on Android Chrome could not reach `https://fxinvestment-mjfdsshe.fly.dev/`
even with VPN. From Devin VM `curl -v https://fxinvestment-mjfdsshe.fly.dev/`
also hung in TLS handshake — Fly machine was auto-stopped + the proxy
appeared regionally unreliable for the user's mobile carrier.

## Solution: static CDN mirror

Built a fully static deploy of the cinematic dashboard on Cloudflare-backed
`devinapps.com`:

- **Permanent URL: https://static-build-seanlntw.devinapps.com/**
- No backend, no cold-start, universal mobile reachability.
- Same cinematic UI (28-pair grid, charts, BUY/SELL pressure, currency
  strength heatmap, filter chips, deep-dive modal, audit page, history).
- All `/api/*` JSON responses are pre-baked at build time and served as
  static files. A fetch-shim (`static-shim.js`) intercepts every
  `fetch("/api/X")` and rewrites it to `./api/X.json` before the request
  leaves the browser.

Trade-off (acceptable per user's "точность важна, не свежесть" stance):
state is frozen at deploy time. Fly continues to run and the Devin VM
schedule continues to push fresh state files into git. Re-running
`scripts/build_static_mirror.sh` + `deploy frontend` re-bakes against
the latest state — this can be wired into the hourly schedule later if
the user wants live-ish refreshes on the static URL.

## Files added / changed

- `scripts/build_static_mirror.sh` — end-to-end builder. Spins up local
  FastAPI on `:8765`, curls every endpoint into `static_build/api/<path>.json`,
  copies the static HTML/CSS/JS, patches asset paths (`/static/X` → `./X`),
  injects `static-shim.js` ahead of the existing `intent.js`/`app.js`.
- `teamagent/dashboard/static/static-shim.js` — the fetch shim. Lives in
  the canonical static dir so future build runs always pick it up.
- `.gitignore` — `static_build/` is a build artifact, don't commit it.

## How to redeploy from any future session

```
bash scripts/build_static_mirror.sh
# then from a Devin session:
# deploy(command="frontend", dir="$REPO_ROOT/static_build")
```

The `deploy frontend` tool returns the same `static-build-seanlntw.devinapps.com`
URL each time as long as the directory name (`static_build`) is preserved.

## Test results (one continuous browser session)

| Test | Result |
|------|--------|
| `/` renders 28-pair cinematic grid | PASS — Top BUY AUDNZD 73%, Top SELL EURCAD 77%, all 28 cards |
| Click `System` → audit dashboard | PASS — 15/15 проверок green, market countdown live |
| Click `History` → closed-trades table | PASS — 10 trades, 6 WIN / 4 LOSS, WR 60%, PnL +$1.10 |
| Click back to `Market Intent` | PASS — grid re-renders, no errors |
| No HTTP/2 protocol errors | PASS — entire flow works |

## Two URLs now in service

| URL | Purpose | Strengths | Weaknesses |
|-----|---------|-----------|------------|
| `https://static-build-seanlntw.devinapps.com/` | static CDN mirror | universal mobile reachability, no cold-start, fast | state frozen at deploy time |
| `https://fxinvestment-mjfdsshe.fly.dev/` | live FastAPI dashboard | live state, full /api endpoints | cold-start 10–15 s, occasional regional unreachability |

User can hand out either. For an Android Chrome reader the static URL is
the primary recommendation.

## Open TODOs

- Optionally wire `bash scripts/build_static_mirror.sh && deploy frontend`
  into the hourly Devin Schedule so the static mirror auto-refreshes its
  baked state every hour. Until that's wired manually, run the build script
  + deploy from any session whenever fresh data is desired.
- Custom domain (e.g. `fxinvestment.com` → CNAME both URLs) blocked on user
  buying the domain. Steps documented in `.agents/skills/fly-deploy/SKILL.md`.
