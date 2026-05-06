"""Self-healing health-check for the live FOREX dashboard.

Pings ``${SITE_URL}/api/signals``.  If response is missing 28 pairs, is
older than 5 minutes, or returns non-200 — sends a Telegram alert.

If FLY_APP_NAME + FLY_API_TOKEN are set, the script also issues
``flyctl machine restart`` as an automatic recovery step.  If those are
not set, it just alerts and exits 0 (CI stays green to avoid alert
spam, the user gets the Telegram message anyway).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def telegram(msg: str) -> None:
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (bot and chat):
        return
    try:
        urlopen(Request(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            data=urlencode({"chat_id": chat, "text": msg}).encode()),
            timeout=10)
    except Exception as e:
        print(f"[health] telegram failed: {e}")


def maybe_restart_fly() -> bool:
    app = os.environ.get("FLY_APP_NAME")
    tok = os.environ.get("FLY_API_TOKEN")
    if not (app and tok):
        print("[health] FLY_APP_NAME / FLY_API_TOKEN not set — alert only")
        return False
    try:
        subprocess.run(
            ["flyctl", "machine", "restart", "--app", app],
            env={**os.environ, "FLY_API_TOKEN": tok},
            check=True, timeout=120,
        )
        print(f"[health] restarted Fly app {app}")
        return True
    except Exception as e:
        print(f"[health] fly restart failed: {e}")
        return False


def main() -> int:
    url = os.environ.get("SITE_URL")
    if not url:
        print("[health] SITE_URL not set — skipping")
        return 0

    api = url.rstrip("/") + "/api/signals"
    now = datetime.now(timezone.utc)
    try:
        with urlopen(api, timeout=20) as r:
            status = r.status
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        telegram(f"🚨 FOREX site DOWN — {e}")
        if maybe_restart_fly():
            telegram(f"🔧 Fly app restarted automatically")
        return 0

    pairs = data.get("pairs", [])
    if status != 200 or len(pairs) < 28:
        telegram(
            f"⚠️ FOREX /api/signals: status={status}, "
            f"pairs={len(pairs)}/28")
        if maybe_restart_fly():
            telegram(f"🔧 Fly app restarted automatically")
        return 0

    last_update = data.get("last_update_utc") or data.get("updated_at")
    if last_update:
        try:
            ts = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
            age_sec = (now - ts).total_seconds()
            if age_sec > 300:                            # 5 min stale
                telegram(f"⚠️ FOREX last update {int(age_sec/60)} мин назад")
                maybe_restart_fly()
        except Exception:
            pass

    print(f"[health] OK ({len(pairs)} pairs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
