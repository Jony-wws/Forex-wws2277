#!/usr/bin/env bash
# windsurf-vnc-setup/setup.sh
# One-shot installer that puts Windsurf onto a Devin VM and exposes a
# combined "VNC + typing field" web UI publicly so the user can drive
# Windsurf from Android Chrome (where the on-screen keyboard otherwise
# drops Cyrillic characters through plain noVNC).
#
# Usage (inside a Devin session):
#   bash windsurf-vnc-setup/setup.sh
#
# Result:
#   - Windsurf 2.2.17 installed and launched on display :0
#   - aiohttp service on port 5050 serving combined.html + WebSocket
#     proxy to local TigerVNC (127.0.0.1:5901) + /type and /key
#     xdotool injection endpoints
#   - Display set to 600x1067 (portrait, phone-friendly)
#   - Russian keyboard layout enabled in X
#   - Public URL printed at the end (run `deploy expose port=5050` to
#     get one — Devin's deploy tool handles tunnel + basic auth)

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NOVNC_VERSION="1.5.0"
NOVNC_DIR="/home/ubuntu/novnc-master"
WINDSURF_URL="https://windsurf-stable.codeiumdata.com/linux-x64-deb/stable/a65d6c4e1fd335336d7a0b601099811667e184ca/Windsurf-linux-x64-2.2.17.deb"

echo "[1/7] Installing Windsurf..."
if ! command -v windsurf >/dev/null 2>&1; then
  curl -fsSL -o /tmp/windsurf.deb "$WINDSURF_URL"
  sudo apt-get update -qq
  sudo apt-get install -y -q /tmp/windsurf.deb
fi

echo "[2/7] Installing helpers (aiohttp, websockify, wmctrl, xdotool)..."
sudo apt-get install -y -q websockify wmctrl scrot
pip3 install --quiet aiohttp

echo "[3/7] Downloading noVNC ${NOVNC_VERSION}..."
if [ ! -d "$NOVNC_DIR" ]; then
  curl -fsSL "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
    -o /tmp/novnc.tar.gz
  tar -xzf /tmp/novnc.tar.gz -C /home/ubuntu/
  mv "/home/ubuntu/noVNC-${NOVNC_VERSION}" "$NOVNC_DIR"
fi
cp "$HERE/combined.html" "$NOVNC_DIR/combined.html"

echo "[4/7] Setting display to 600x1067 portrait + Russian layout..."
DISPLAY=:0 xrandr --newmode 600x1067 41.50 600 632 680 760 1067 1070 1080 1100 -hsync +vsync 2>/dev/null || true
DISPLAY=:0 xrandr --addmode VNC-0 600x1067 2>/dev/null || true
DISPLAY=:0 xrandr --output VNC-0 --mode 600x1067 || true
DISPLAY=:0 setxkbmap -layout "us,ru" -option "grp:alt_shift_toggle" || true

echo "[5/7] Launching Windsurf if not running..."
if ! pgrep -f '/usr/share/windsurf/windsurf' >/dev/null; then
  DISPLAY=:0 nohup windsurf --no-sandbox --password-store=basic \
    > /tmp/windsurf.log 2>&1 &
  sleep 5
fi
WID="$(DISPLAY=:0 wmctrl -l | awk '$NF == "Windsurf" {print $1; exit}')"
if [ -n "$WID" ]; then
  DISPLAY=:0 wmctrl -i -r "$WID" -b add,maximized_vert,maximized_horz || true
fi

echo "[6/7] Starting combined server (port 5050)..."
mkdir -p /home/ubuntu/typebridge
cp "$HERE/combined.py" /home/ubuntu/typebridge/combined.py
pkill -f 'typebridge/combined.py' 2>/dev/null || true
sleep 1
nohup python3 /home/ubuntu/typebridge/combined.py > /tmp/combined.log 2>&1 &
sleep 2

echo "[7/7] Verifying..."
curl -fsS -I http://localhost:5050/combined.html | head -1
curl -fsS -I http://localhost:5050/core/rfb.js   | head -1
echo "OK. Now run inside Devin:  deploy expose port=5050"
echo "Then send the printed URL to the user (Android Chrome will accept user:pass@host)."
