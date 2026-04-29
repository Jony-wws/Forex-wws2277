#!/bin/bash
set -e
cd "$(dirname "$0")"

# Install deps if missing
python -c "import websocket, pandas, yfinance, requests, dateutil" 2>/dev/null || \
  pip install -q -r requirements.txt

# Token comes from env (DERIV_DEMO_TOKEN)
if [ -z "$DERIV_DEMO_TOKEN" ]; then
  echo "ERROR: DERIV_DEMO_TOKEN env var not set"
  exit 1
fi

# Run one tick of the v17 bot
mkdir -p logs
python deriv_v17_pro.py
echo "[run_once] tick complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
