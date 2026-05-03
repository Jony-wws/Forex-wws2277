---
name: fly-deploy
description: |
  How to (re)deploy the FX INVESTMENT TeamAgent dashboard to Fly.io as a
  permanent, free, no-auth public URL using Devin's `deploy backend` tool.
  Use this when the user asks for "fly deploy", "permanent URL", "fxinvestment
  on fly", "site that works without me", or anything similar.
---

# Fly.io permanent deploy — FX INVESTMENT

## Live URL (canonical)

`https://fxinvestment-vsxcxrqj.fly.dev/`

If a redeploy assigns a different subdomain, update **both** this file
and `AGENTS.md` "Where to find the user's data".

## Architecture

The Fly machine runs the **dashboard only** — it auto-detects Fly via the
`/data` mount (or `FLY_APP_NAME` env var) and skips spawning the
orchestrator + 60 subprocess agents (which would OOM-kill on the 256-MB
free tier).

The trading loop (forecast_scanner, paper_trader, paper_trader_daily,
strategy_meta_agent, market_radar, etc.) runs on the Devin VM via the
**hourly Schedule** `sched-083b11171a0841668f4608b075d769b5`. State files
are committed to git by `state_committer` every 15 min. Every Fly
redeploy picks up the latest committed state from
`/app/teamagent/state/*.json` and seeds the persistent volume on first
boot.

This split (heavy compute on Devin VM, lightweight serving on Fly)
matches AGENTS.md "Deployment & permanent URL" exactly.

## Step-by-step redeploy

```bash
# 1. Always start from the canonical branch.
cd /home/ubuntu/repos/Forex-wws2277
git checkout devin/1777586006-teamagent-rebuild
git pull --rebase origin devin/1777586006-teamagent-rebuild

# 2. (Optional) refresh state files from a quick local scan, so the new
#    Fly image ships up-to-the-minute forecasts. This is purely a UX
#    nicety — the hourly schedule will refresh state anyway.
bash scripts/start_all.sh
sleep 600   # 10 min: scanner + paper_trader + state_committer commit
bash scripts/stop_all.sh

# 3. Use Devin's deploy backend tool. It auto-generates Dockerfile + fly.toml.
#    Keep the volume so /data/state persists across deploys.
deploy backend --dir /home/ubuntu/repos/Forex-wws2277 --volume true
```

After the deploy finishes, verify with:

```bash
curl -sI https://fxinvestment-vsxcxrqj.fly.dev/intent          # 200 OK
curl -s  https://fxinvestment-vsxcxrqj.fly.dev/api/_debug | jq # state listing
curl -s  https://fxinvestment-vsxcxrqj.fly.dev/api/forecasts | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d.get('scanned_at'), len(d.get('forecasts',{})))"
```

You should see `scanned_at` set and 28 pairs.

## Critical files (do NOT regress)

If a future agent edits any of these, run a deploy + curl check:

- `pyproject.toml` — full deps, all subpackages listed, `state/*.json`
  in package-data. Without all of these, `deploy backend` ships a
  half-baked image.
- `.dockerignore` — keep `state/*.json` INCLUDED. Only volatile files
  (`heartbeat_*.json`, `agent_*.json`, `archive/`, `dukascopy_cache/`)
  excluded.
- `teamagent/config.py` — `STATE_DIR` and `LOGS_DIR` MUST honor the env
  vars `TEAMAGENT_STATE_DIR` / `TEAMAGENT_LOGS_DIR` (Fly volume).
- `teamagent/dashboard/server.py` — lifespan event runs
  `_seed_state_files()` (cold-boot bootstrap) + `_spawn_supervisor_processes()`
  (auto-detects Fly → dashboard-only). Do not regress this to a manual
  `if __name__ == "__main__"` server.

## Why not the hand-written `infra/fly/Dockerfile`?

Devin's `deploy backend` tool **does not** use it. The tool inspects
`pyproject.toml`, generates its own slim Dockerfile (uv-based) and its
own fly.toml. Keeping the hand-written `infra/fly/*` files around is
fine for documentation / fly CLI fallback, but the Devin tool ignores
them. Test changes via `deploy backend`, not via `fly deploy`.

## Cold-start behaviour

Fly auto-stops the machine after ~1 minute of no traffic to save free-tier
quota. The first request after idle takes ~10–20 sec to warm up
(Firecracker boot + FastAPI startup). Subsequent requests are instant.

If the user complains about slow first load, options:
1. Tell them this is by design (free tier saves quota).
2. Upgrade to ≥1 GB machine + `min_machines_running = 1` (needs flyctl
   + `FLY_API_TOKEN`).

## Custom domain (`fxinvestment.com`)

Paid (~$10–15/year at Namecheap, Cloudflare, etc.). Steps if the user
buys one:

```bash
fly certs add fxinvestment.com -a fxinvestment-vsxcxrqj
# user adds CNAME @ -> fxinvestment-vsxcxrqj.fly.dev at registrar
fly certs check fxinvestment.com -a fxinvestment-vsxcxrqj   # wait for green
```

Until then, `fxinvestment-vsxcxrqj.fly.dev` is the free permanent URL.
