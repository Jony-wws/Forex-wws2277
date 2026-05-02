#!/usr/bin/env bash
# Запуск всех компонентов TeamAgent.
set -euo pipefail
cd "$(dirname "$0")/.."

# зависимости
python -c "import fastapi, yfinance, pandas" 2>/dev/null || \
  pip install -q -r teamagent/requirements.txt

mkdir -p teamagent/logs teamagent/state

# запускаем 3 фоновых процесса:
#   1. orchestrator (он сам поднимет forecast_scanner + paper_trader + 60 агентов)
#   2. watchdog
#   3. dashboard

ROOT="$(pwd)"
LOG="$ROOT/teamagent/logs"

# orchestrator
nohup python -m teamagent.orchestrator > "$LOG/orchestrator.out" 2> "$LOG/orchestrator.err" &
echo "[start_all] orchestrator pid=$!"
sleep 1

# watchdog
nohup python -m teamagent.watchdog > "$LOG/watchdog.out" 2> "$LOG/watchdog.err" &
echo "[start_all] watchdog pid=$!"

# dashboard
nohup python -m teamagent.dashboard.server > "$LOG/dashboard.out" 2> "$LOG/dashboard.err" &
echo "[start_all] dashboard pid=$!"

sleep 2
echo "[start_all] all started; tail logs:  tail -f teamagent/logs/*.log"
