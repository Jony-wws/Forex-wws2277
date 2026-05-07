#!/usr/bin/env bash
# windsurf-vnc-setup/setup.sh
#
# One-shot installer that puts Windsurf onto a Devin VM and exposes a
# combined "VNC + full keyboard + full mouse + clipboard" web UI publicly
# so the user can drive the VM from Android Chrome (where the on-screen
# keyboard otherwise drops Cyrillic characters through plain noVNC).
#
# Usage (inside a Devin session):
#   bash windsurf-vnc-setup/setup.sh
#
# Result:
#   - Helpers (aiohttp, websockify, wmctrl, xdotool, xclip, xdg-utils)
#   - noVNC 1.5.0 unpacked under /home/ubuntu/novnc-master/
#   - combined.html copied next to noVNC core (so 'import RFB from
#     ./core/rfb.js' resolves)
#   - Windsurf 2.2.17 installed and launched on display :0 (best effort —
#     the rest of the UI works even without it; setup will not fail if
#     Windsurf can't be installed in this environment)
#   - Display set to 600x1067 portrait (phone-friendly), Russian xkb layout
#   - aiohttp service on port 5050 serving combined.html + WebSocket
#     proxy to local TigerVNC (127.0.0.1:5901) + /type /key /click /scroll
#     /drag /clipboard_get /clipboard_set /clipboard_copy_active /openurl
#     /focus_windsurf /focus_chat /screen_info
#   - Public URL printed at the end (run `deploy expose port=5050` to
#     get one — Devin's deploy tool handles tunnel + basic auth)

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NOVNC_VERSION="1.5.0"
NOVNC_DIR="/home/ubuntu/novnc-master"
WINDSURF_URL="https://windsurf-stable.codeiumdata.com/linux-x64-deb/stable/a65d6c4e1fd335336d7a0b601099811667e184ca/Windsurf-linux-x64-2.2.17.deb"

echo "[1/7] Installing helpers (aiohttp, wmctrl, xdotool, xclip, xdg-utils)..."
sudo apt-get update -qq || true
sudo apt-get install -y -q wmctrl scrot xclip xdg-utils ca-certificates curl || true
pip3 install --quiet aiohttp || pip install --quiet aiohttp || true

echo "[2/7] Downloading noVNC ${NOVNC_VERSION} ..."
if [ ! -d "$NOVNC_DIR" ]; then
  curl -fsSL "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
    -o /tmp/novnc.tar.gz
  tar -xzf /tmp/novnc.tar.gz -C /home/ubuntu/
  mv "/home/ubuntu/noVNC-${NOVNC_VERSION}" "$NOVNC_DIR"
fi
cp -f "$HERE/combined.html" "$NOVNC_DIR/combined.html"

echo "[3/7] Setting display 600x1067 + Russian layout (best effort)..."
DISPLAY=:0 xrandr --newmode 600x1067 41.50 600 632 680 760 1067 1070 1080 1100 -hsync +vsync 2>/dev/null || true
DISPLAY=:0 xrandr --addmode VNC-0 600x1067 2>/dev/null || true
DISPLAY=:0 xrandr --output VNC-0 --mode 600x1067 2>/dev/null || true
DISPLAY=:0 setxkbmap -layout "us,ru" -option "grp:alt_shift_toggle" 2>/dev/null || true

echo "[4/7] Installing Windsurf (best effort) ..."
if ! command -v windsurf >/dev/null 2>&1; then
  if curl -fsSL -o /tmp/windsurf.deb "$WINDSURF_URL"; then
    sudo apt-get install -y -q /tmp/windsurf.deb 2>/dev/null || true
  fi
fi

echo "[5/7] Launching Windsurf if not running (best effort) ..."
if command -v windsurf >/dev/null 2>&1 && ! pgrep -f '/usr/share/windsurf/windsurf' >/dev/null; then
  DISPLAY=:0 nohup windsurf --no-sandbox --password-store=basic \
    > /tmp/windsurf.log 2>&1 &
  sleep 5
fi
if command -v wmctrl >/dev/null 2>&1; then
  WID="$(DISPLAY=:0 wmctrl -l 2>/dev/null | awk '$NF == "Windsurf" {print $1; exit}')" || true
  if [ -n "${WID:-}" ]; then
    DISPLAY=:0 wmctrl -i -r "$WID" -b add,maximized_vert,maximized_horz || true
  fi
fi

echo "[6/7] Starting combined server (port 5050) ..."
mkdir -p /home/ubuntu/typebridge
cp -f "$HERE/combined.py" /home/ubuntu/typebridge/combined.py
pkill -f 'typebridge/combined.py' 2>/dev/null || true
sleep 1
nohup python3 /home/ubuntu/typebridge/combined.py > /tmp/combined.log 2>&1 &
sleep 2

echo "[7/7] Verifying ..."
curl -fsS -I http://localhost:5050/combined.html | head -1
curl -fsS -I http://localhost:5050/core/rfb.js   | head -1
curl -fsS    http://localhost:5050/screen_info   | head -1 || true

echo
echo "Готово. Дальше внутри сессии Devin:"
echo "  deploy expose port=5050"
echo "Полученный URL отдай пользователю в Android Chrome."
