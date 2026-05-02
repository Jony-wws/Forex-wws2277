#!/usr/bin/env bash
# FX INVESTMENT — Fly.io container entrypoint.
# Spawns orchestrator + watchdog in background, dashboard in foreground.
# All three processes share /data (Fly volume) for teamagent/state files.
set -euo pipefail

cd /app

mkdir -p /data/state /data/logs

# Mirror state symlink into the repo so existing teamagent.config.STATE_DIR
# resolution (which currently points to repo/teamagent/state) finds the
# persistent volume. Symlink is idempotent and survives restarts.
if [ ! -L /app/teamagent/state ] || [ "$(readlink /app/teamagent/state)" != "/data/state" ]; then
  rm -rf /app/teamagent/state
  ln -s /data/state /app/teamagent/state
fi
if [ ! -L /app/teamagent/logs ] || [ "$(readlink /app/teamagent/logs)" != "/data/logs" ]; then
  rm -rf /app/teamagent/logs
  ln -s /data/logs /app/teamagent/logs
fi

LOG=/data/logs

# Seed empty state files on cold-boot so dashboard works before
# orchestrator's first sweep completes.
python - <<'PY'
import json, os, pathlib
p = pathlib.Path("/data/state")
p.mkdir(parents=True, exist_ok=True)
seeds = {
  "forecasts.json": {"forecasts": {}, "rankings": [], "scanned_at": None},
  "market_radar.json": {"pairs": {}, "scanners": [], "as_of": None},
  "cot.json": {"currencies": {}, "as_of": None},
  "open_trades.json": {"trades": []},
  "stakan_open_trades_enriched.json": {"trades": []},
  "stakan_signals.json": {"signals": [], "as_of": None},
  "daily_signals.json": {"signals": [], "as_of": None},
}
for fn, payload in seeds.items():
  fp = p / fn
  if not fp.exists():
    fp.write_text(json.dumps(payload, ensure_ascii=False))
    print(f"[seed] {fn}")
PY

echo "[start] orchestrator…"
nohup python -m teamagent.orchestrator > "$LOG/orchestrator.out" 2> "$LOG/orchestrator.err" &
ORCH_PID=$!
echo "  pid=$ORCH_PID"

sleep 1

echo "[start] watchdog…"
nohup python -m teamagent.watchdog > "$LOG/watchdog.out" 2> "$LOG/watchdog.err" &
WD_PID=$!
echo "  pid=$WD_PID"

sleep 1

# Trap signals to forward them to children, then exec dashboard in foreground.
trap "echo '[stop] forwarding to children'; kill -TERM $ORCH_PID $WD_PID 2>/dev/null || true; wait" SIGTERM SIGINT

echo "[start] dashboard (foreground)…"
exec python -m teamagent.dashboard.server
