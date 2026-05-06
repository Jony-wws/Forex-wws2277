"""Tiny Telegram bot for the FOREX 28 dashboard Mini App.

Single-file long-poll bot using only the standard library + ``requests``.
On ``/start`` it replies with an inline keyboard containing one
``web_app`` button that opens the dashboard inside Telegram (Mini App).

Designed to run from a free GitHub Actions cron job (see
``.github/workflows/telegram_bot_keepalive.yml``) for a few minutes per
invocation — that is enough for an interactive user to press
``/start`` and receive the button.

Environment:
    TELEGRAM_BOT_TOKEN  — token from @BotFather (required).
    DASHBOARD_URL       — public HTTPS URL of the dashboard (required).
                          The bot will append ``/tg`` if not already
                          present so the WebApp SDK is loaded.
    RUN_SECONDS         — how many seconds to long-poll before exiting
                          (default 270 — under the 5-minute workflow
                          step budget).
    POLL_TIMEOUT        — long-poll timeout per request (default 25).

Why no ``python-telegram-bot``?  We deliberately avoid heavy deps so the
script starts in <1 s on a free runner and the workflow stays under the
keepalive budget.  ``requests`` is already part of every standard
GitHub Actions Python image.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

try:
    import requests
except Exception as exc:  # pragma: no cover - import-time guard
    print(f"telegram_bot: missing 'requests' dependency: {exc}", file=sys.stderr)
    raise

API = "https://api.telegram.org/bot{token}/{method}"

log = logging.getLogger("telegram_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _api(token: str, method: str, **params: Any) -> dict:
    """Call a Telegram Bot API method and return the parsed JSON body."""
    url = API.format(token=token, method=method)
    try:
        resp = requests.post(url, json=params, timeout=30)
        return resp.json()
    except requests.RequestException as e:
        log.warning("api %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}


def _mini_app_url(dashboard_url: str) -> str:
    """Make sure the URL points at the ``/tg`` Mini App route."""
    url = dashboard_url.rstrip("/")
    if not url.endswith("/tg"):
        url = f"{url}/tg"
    return url


def _send_start_reply(token: str, chat_id: int, mini_app_url: str) -> None:
    """Reply to /start with an inline ``web_app`` button."""
    text = (
        "FOREX Сигналы 2026 — 28 валютных пар, обновление каждые 10 секунд.\n"
        "Нажмите кнопку ниже, чтобы открыть дашборд прямо в Telegram."
    )
    keyboard = {
        "inline_keyboard": [[
            {
                "text": "Открыть FOREX 28",
                "web_app": {"url": mini_app_url},
            }
        ]]
    }
    res = _api(
        token,
        "sendMessage",
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    if not res.get("ok"):
        log.warning("sendMessage failed: %s", res)


def _handle_update(token: str, mini_app_url: str, update: dict) -> None:
    """Process a single update from getUpdates."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return
    if text.startswith("/start") or text.startswith("/forex") or text == "/help":
        _send_start_reply(token, int(chat_id), mini_app_url)


def run(
    token: str,
    dashboard_url: str,
    run_seconds: int = 270,
    poll_timeout: int = 25,
) -> int:
    """Long-poll for ``run_seconds`` then exit cleanly.

    Returns an exit code: 0 on normal exit, 1 on fatal config error.
    """
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is empty — refusing to start")
        return 1
    if not dashboard_url:
        log.error("DASHBOARD_URL is empty — refusing to start")
        return 1

    mini_app_url = _mini_app_url(dashboard_url)
    log.info("Mini App URL: %s", mini_app_url)

    me = _api(token, "getMe")
    if not me.get("ok"):
        log.error("getMe failed: %s", me)
        return 1
    bot_user = me.get("result", {}).get("username", "?")
    log.info("Bot @%s online; long-polling for %ss", bot_user, run_seconds)

    deadline = time.time() + max(10, int(run_seconds))
    offset: int | None = None

    while time.time() < deadline:
        params: dict[str, Any] = {"timeout": poll_timeout}
        if offset is not None:
            params["offset"] = offset
        res = _api(token, "getUpdates", **params)
        if not res.get("ok"):
            log.warning("getUpdates not ok: %s", res)
            time.sleep(2)
            continue
        for upd in res.get("result", []):
            try:
                _handle_update(token, mini_app_url, upd)
            except Exception as e:
                log.warning("update handler failed: %s", e)
            offset = int(upd["update_id"]) + 1

    log.info("Run window over; bye")
    return 0


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()
    run_seconds = int(os.environ.get("RUN_SECONDS", "270") or "270")
    poll_timeout = int(os.environ.get("POLL_TIMEOUT", "25") or "25")

    if not token:
        log.warning("TELEGRAM_BOT_TOKEN missing — exiting cleanly (skip)")
        return 0
    if not dashboard_url:
        log.warning("DASHBOARD_URL missing — exiting cleanly (skip)")
        return 0

    return run(token, dashboard_url, run_seconds=run_seconds, poll_timeout=poll_timeout)


if __name__ == "__main__":
    sys.exit(main())
