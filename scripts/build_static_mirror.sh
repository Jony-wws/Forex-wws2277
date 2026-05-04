#!/usr/bin/env bash
# Build a static (CDN-friendly) mirror of the FX INVESTMENT dashboard.
#
# Why: the Fly.io free-tier deploy (https://fxinvestment-vsxcxrqj.fly.dev/) is
# unreachable from some mobile carriers / regions even with VPN — the user
# explicitly hit ERR_HTTP2_PROTOCOL_ERROR / "site can't be reached" on Android
# Chrome. A static deploy on devinapps.com (Cloudflare CDN) bypasses that:
# universal mobile reachability, no cold-start, no backend.
#
# How it works:
#   1. Spin up the FastAPI dashboard locally on :8765 (DASHBOARD_ONLY=1).
#   2. Curl every endpoint into static_build/api/<path>.json.
#   3. Copy the static HTML/CSS/JS, fix /static/ → relative paths.
#   4. Drop static_build/static-shim.js which intercepts fetch(/api/X) and
#      rewrites it to ./api/X.json before the request leaves the browser.
#   5. Deploy static_build/ via the Devin `deploy frontend` tool (called by
#      a Devin session — this script does NOT call it itself).
#
# Usage: bash scripts/build_static_mirror.sh
#        # then from a Devin session:
#        # deploy(command="frontend", dir="$REPO_ROOT/static_build")

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT="$ROOT/static_build"
SRC="$ROOT/teamagent/dashboard/static"
PORT=8765
BASE="http://127.0.0.1:$PORT"
PAIRS="EURUSD GBPUSD USDJPY USDCHF AUDUSD NZDUSD USDCAD EURGBP EURJPY GBPJPY EURCHF GBPCHF AUDJPY CADJPY CHFJPY NZDJPY AUDCAD AUDCHF AUDNZD CADCHF EURAUD EURCAD EURNZD GBPAUD GBPCAD GBPNZD NZDCAD NZDCHF"

echo "== 1/5 ensure deps installed =="
pip install -q "fastapi[standard]" "uvicorn[standard]" yfinance pandas numpy 2>&1 | tail -2 || true

echo "== 2/5 start local dashboard =="
pkill -f "uvicorn teamagent" 2>/dev/null || true
sleep 1
DASHBOARD_ONLY=1 nohup python -m uvicorn teamagent.dashboard.server:app \
  --host 127.0.0.1 --port "$PORT" > /tmp/static_mirror_build.log 2>&1 &
DPID=$!
trap "kill $DPID 2>/dev/null || true" EXIT
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  if curl -sf --max-time 2 "$BASE/api/health" > /dev/null; then break; fi
done
curl -sf --max-time 5 "$BASE/api/health" > /dev/null || { echo "ERROR: dashboard didn't start"; tail -30 /tmp/static_mirror_build.log; exit 1; }

echo "== 3/5 reset $OUT =="
rm -rf "$OUT"
mkdir -p "$OUT/api"/{intent-bars,forecast,microstructure,stakan,stakan-view,daily,stability-forecast,volume-profile,meta-strategy,market-radar,market-regime,regime,analyst,stability}

echo "== 4/5 bake /api/* responses =="
# Top-level endpoints. fundamentals / market-regime / weekly-loss-review /
# wr-floor are referenced from app.js — without them the System tab logs
# `SyntaxError: Unexpected token '<'` because the SPA fallback returns
# index.html for the missing JSON path.
for ep in forecasts market-radar cot open-trades closed-trades stats agents backtest health \
          strategy-config market-status system-audit system-health meta-strategy stability \
          fundamentals market-regime weekly-loss-review wr-floor min-guarantee \
          risk-metrics calibration agent-reports coverage-matrix final-signal final-signals ai-narrative \
          playbook analyst daily-target stakan-view; do
  # /api/agent-reports does live RSS fetches — give it more time.
  # /api/final-signal in turn calls all_reports() so it also needs a long timeout.
  # /api/analyst calls regime classification on 28 pairs — needs ~60-90s.
  case "$ep" in
    agent-reports|final-signal|final-signals|ai-narrative) timeout=45 ;;
    analyst)                    timeout=120 ;;
    *)                          timeout=12 ;;
  esac
  curl -sf --max-time "$timeout" "$BASE/api/$ep" > "$OUT/api/${ep}.json" || echo "  WARN $ep"
done
# per-pair regime + analyst (cheap calls — bake them too so static mirror can
# show live mood without hitting backend).
for p in $PAIRS; do
  curl -sf --max-time 25 "$BASE/api/regime/$p"  > "$OUT/api/regime/${p}.json"  2>/dev/null || true
  curl -sf --max-time 25 "$BASE/api/analyst/$p" > "$OUT/api/analyst/${p}.json" 2>/dev/null || true
done
for ep in stakan/open-trades stakan/signals stakan/stats stakan/closed-trades \
          daily/signals daily/stats daily/open-trades \
          daily/closed-trades daily/paused; do
  curl -sf --max-time 12 "$BASE/api/$ep" > "$OUT/api/${ep}.json" || echo "  WARN $ep"
done
for h in 1 6 24; do
  curl -sf --max-time 12 "$BASE/api/stability-forecast?hours_ahead=$h" \
    > "$OUT/api/stability-forecast/${h}.json" || true
done
curl -sf --max-time 12 "$BASE/api/meta-strategy/log?limit=20" \
  > "$OUT/api/meta-strategy/log.json" || true
for p in $PAIRS; do
  curl -sf --max-time 12 "$BASE/api/intent-bars/$p?interval=15m&n=90" > "$OUT/api/intent-bars/${p}.json" || true
  curl -sf --max-time 8  "$BASE/api/forecast/$p"        > "$OUT/api/forecast/${p}.json" 2>/dev/null || true
  curl -sf --max-time 8  "$BASE/api/microstructure/$p"  > "$OUT/api/microstructure/${p}.json" 2>/dev/null || true
  curl -sf --max-time 8  "$BASE/api/volume-profile/$p"  > "$OUT/api/volume-profile/${p}.json" 2>/dev/null || true
  curl -sf --max-time 12 "$BASE/api/stakan-view/$p"     > "$OUT/api/stakan-view/${p}.json" 2>/dev/null || true
done

echo "== 5/5 copy + patch HTML/JS/CSS =="
cp "$SRC/intent.html"  "$OUT/index.html"
cp "$SRC/index.html"   "$OUT/system.html"
cp "$SRC/trades.html"  "$OUT/trades.html"
cp "$SRC/intent.css"   "$OUT/intent.css"
cp "$SRC/style.css"    "$OUT/style.css"
cp "$SRC/intent.js"    "$OUT/intent.js"
cp "$SRC/app.js"       "$OUT/app.js"
cp "$SRC/trades.js"    "$OUT/trades.js"
cp "$SRC/fx-ux.js"     "$OUT/fx-ux.js"
cp "$SRC/static-shim.js" "$OUT/static-shim.js"

# Inline lightweight-charts so the static deploy has zero external CDN deps.
# When unpkg.com is slow / cache-validating / blocked by ISP, the page used
# to hang on second visit waiting for the chart library.
if [ -f "$SRC/lightweight-charts.standalone.production.js" ]; then
  cp "$SRC/lightweight-charts.standalone.production.js" "$OUT/lightweight-charts.standalone.production.js"
else
  curl -sSL --max-time 30 \
    "https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js" \
    -o "$OUT/lightweight-charts.standalone.production.js"
fi

# Fix asset paths and tab links in all three HTML files.
for f in "$OUT/index.html" "$OUT/system.html" "$OUT/trades.html"; do
  sed -i \
    -e 's|"/static/style.css"|"./style.css"|g' \
    -e 's|"/static/intent.css"|"./intent.css"|g' \
    -e 's|"/static/fx-ux.js"|"./fx-ux.js"|g' \
    -e 's|"/static/intent.js"|"./static-shim.js"></script>\n<script src="./intent.js"|g' \
    -e 's|"/static/app.js"|"./static-shim.js"></script>\n<script src="./app.js"|g' \
    -e 's|"/static/trades.js"|"./trades.js"|g' \
    -e 's|"/static/static-shim.js"|"./static-shim.js"|g' \
    -e 's|href="/intent"|href="./"|g' \
    -e 's|href="/trades"|href="./trades.html"|g' \
    -e 's|href="/system"|href="./system.html"|g' \
    -e 's|href="/agents"|href="./system.html#agents-section"|g' \
    -e 's|href="/history"|href="./trades.html"|g' \
    "$f"
done

kill $DPID 2>/dev/null || true
trap - EXIT

echo ""
echo "Done. Bundle size: $(du -sh "$OUT" | cut -f1)"
echo "JSON files baked: $(find "$OUT/api" -name '*.json' | wc -l)"
echo ""
echo "Next step (run from a Devin session):"
echo "  deploy(command=\"frontend\", dir=\"$OUT\")"
