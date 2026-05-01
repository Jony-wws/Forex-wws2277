#!/usr/bin/env bash
# Останавливаем всё.
set -uo pipefail

stop_proc () {
  local name="$1"
  local pids
  pids=$(pgrep -f "$name" || true)
  if [ -n "$pids" ]; then
    echo "[stop_all] killing $name pids=$pids"
    kill -TERM $pids 2>/dev/null || true
    sleep 2
    kill -KILL $pids 2>/dev/null || true
  fi
}

stop_proc "teamagent.orchestrator"
stop_proc "teamagent.watchdog"
stop_proc "teamagent.dashboard.server"
stop_proc "teamagent.forecast_scanner"
stop_proc "teamagent.paper_trader"
stop_proc "teamagent.agents._runner"

echo "[stop_all] done"
