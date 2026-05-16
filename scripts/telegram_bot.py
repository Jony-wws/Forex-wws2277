"""Tiny Telegram bot for the FOREX 28 dashboard Mini App + agent mode.

Single-file long-poll bot using only the standard library + ``requests``.

Two response paths:

1. **Slash commands** ``/start`` / ``/forex`` / ``/help`` reply with an
   inline keyboard containing a ``web_app`` button that opens the
   dashboard inside Telegram (Mini App).

2. **Free text / other commands** are sent to :mod:`telegram_agent`,
   which (a) understands the request via GitHub Models, (b) optionally
   triggers a GitHub Actions workflow through the REST API.  The bot
   echoes the agent's confirmation back to the user; the workflow itself
   posts results to the chat once it finishes.

Designed to run from a free GitHub Actions cron job (see
``.github/workflows/telegram_bot_keepalive.yml``) for a few minutes per
invocation — that is enough for an interactive user to press
``/start`` and receive the button.

Environment:
    TELEGRAM_BOT_TOKEN      — token from @BotFather (required).
    DASHBOARD_URL           — public HTTPS URL of the dashboard
                              (required for /start, optional otherwise).
                              The bot will append ``/tg`` if not already
                              present so the WebApp SDK is loaded.
    RUN_SECONDS             — how many seconds to long-poll before
                              exiting (default 270 — under the
                              5-minute workflow step budget).
    POLL_TIMEOUT            — long-poll timeout per request
                              (default 25).
    GITHUB_TOKEN            — used by telegram_agent for free-tier
                              GitHub Models (intent parsing).  Optional
                              — bot falls back to a regex heuristic
                              parser when missing.
    GH_DISPATCH_TOKEN       — PAT with ``actions:write`` so the bot can
                              trigger workflows in ``GITHUB_REPOSITORY``
                              via ``POST /actions/workflows/{f}/
                              dispatches``.  Optional — without it the
                              bot still parses intents and replies, but
                              cannot run anything.
    GITHUB_REPOSITORY       — ``owner/repo`` to dispatch workflows in.
    GITHUB_REF_NAME         — branch to run the workflow on (default
                              ``main``).
    TELEGRAM_ALLOWED_CHATS  — comma-separated whitelist of chat ids
                              allowed to use the agent.  When set, all
                              other chats only get the Mini App button.
                              Leave unset to allow every chat (use at
                              your own risk).

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

# Local import — the agent module lives next to this file.
try:
    from telegram_agent import parse_and_dispatch
except Exception as exc:  # pragma: no cover - allow bot to keep running without agent
    print(f"telegram_bot: telegram_agent unavailable: {exc}", file=sys.stderr)
    parse_and_dispatch = None  # type: ignore[assignment]

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


def _send_text(token: str, chat_id: int, text: str) -> None:
    """Plain text reply with no markup."""
    res = _api(
        token,
        "sendMessage",
        chat_id=chat_id,
        text=text,
        disable_web_page_preview=True,
    )
    if not res.get("ok"):
        log.warning("sendMessage failed: %s", res)


def _send_start_reply(token: str, chat_id: int, mini_app_url: str | None) -> None:
    """Reply to ``/start`` with the Mini App button (or fall back to text)."""
    text = (
        "FOREX Сигналы 2026 — 28 валютных пар, обновление каждые 10 секунд.\n"
        "Нажмите кнопку ниже, чтобы открыть дашборд прямо в Telegram."
    )
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if mini_app_url:
        params["reply_markup"] = {
            "inline_keyboard": [[
                {
                    "text": "Открыть FOREX 28",
                    "web_app": {"url": mini_app_url},
                }
            ]]
        }
    else:
        params["text"] = (
            text + "\n(Mini App не настроен — DASHBOARD_URL пустой)"
        )
    res = _api(token, "sendMessage", **params)
    if not res.get("ok"):
        log.warning("sendMessage failed: %s", res)


def _agent_reply(
    token: str,
    chat_id: int,
    user_text: str,
    *,
    github_token: str | None,
    dispatch_token: str | None,
    repo: str | None,
    ref: str,
) -> None:
    """Run the agent for ``user_text`` and post its reply back to ``chat_id``."""
    if parse_and_dispatch is None:
        _send_text(token, chat_id, "Агент недоступен (модуль telegram_agent не подгрузился).")
        return

    try:
        result = parse_and_dispatch(
            user_text,
            github_token=github_token,
            dispatch_token=dispatch_token,
            repo=repo,
            ref=ref,
        )
    except Exception as e:  # pragma: no cover - defensive
        log.warning("agent crashed on %r: %s", user_text[:80], e)
        _send_text(token, chat_id, f"Внутренняя ошибка агента: {e}")
        return

    reply = result.get("reply") or "…"
    error = result.get("error")
    run_url = result.get("run_url")
    dispatched = result.get("dispatched")

    parts: list[str] = [reply]
    if dispatched and run_url:
        parts.append(f"\n▶ Запуск: {run_url}")
        parts.append("Отчёт пришлю как только workflow закончит.")
    elif error:
        parts.append(f"\n⚠ {error}")
    _send_text(token, chat_id, "\n".join(parts))


def _chat_allowed(chat_id: int, allowed: set[int] | None) -> bool:
    """Whitelist check. ``None`` means allow every chat."""
    if allowed is None:
        return True
    return chat_id in allowed


def _handle_update(
    token: str,
    mini_app_url: str | None,
    update: dict,
    *,
    allowed_chats: set[int] | None,
    github_token: str | None,
    dispatch_token: str | None,
    repo: str | None,
    ref: str,
) -> None:
    """Process a single update from ``getUpdates``."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return
    chat_id_int = int(chat_id)

    # /start, /forex, /help — keep the Mini App / help reply.
    if text.startswith("/start") or text.startswith("/forex"):
        _send_start_reply(token, chat_id_int, mini_app_url)
        return

    # No text → nothing to do (stickers, photos, etc.).
    if not text:
        return

    # Chat allow-list (only for agent mode — /start stays public).
    if not _chat_allowed(chat_id_int, allowed_chats):
        log.info("chat %s not in whitelist; ignoring %r", chat_id_int, text[:60])
        return

    # Everything else → agent.
    _agent_reply(
        token,
        chat_id_int,
        text,
        github_token=github_token,
        dispatch_token=dispatch_token,
        repo=repo,
        ref=ref,
    )


def _parse_allowed_chats(raw: str) -> set[int] | None:
    """Parse ``TELEGRAM_ALLOWED_CHATS`` env var. Empty → ``None`` (allow all)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    out: set[int] = set()
    for piece in raw.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.add(int(piece))
        except ValueError:
            log.warning("ignoring non-numeric chat id in whitelist: %r", piece)
    return out or None


def run(
    token: str,
    dashboard_url: str,
    run_seconds: int = 270,
    poll_timeout: int = 25,
    *,
    allowed_chats: set[int] | None = None,
    github_token: str | None = None,
    dispatch_token: str | None = None,
    repo: str | None = None,
    ref: str = "main",
) -> int:
    """Long-poll for ``run_seconds`` then exit cleanly.

    Returns an exit code: 0 on normal exit, 1 on fatal config error.
    """
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is empty — refusing to start")
        return 1

    mini_app_url = _mini_app_url(dashboard_url) if dashboard_url else None
    if mini_app_url:
        log.info("Mini App URL: %s", mini_app_url)
    else:
        log.info("DASHBOARD_URL empty — /start replies in text-only mode")

    log.info(
        "agent: gh_models=%s, dispatch=%s, repo=%s, allowed_chats=%s",
        bool(github_token),
        bool(dispatch_token),
        repo or "—",
        "all" if allowed_chats is None else sorted(allowed_chats),
    )

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
                _handle_update(
                    token,
                    mini_app_url,
                    upd,
                    allowed_chats=allowed_chats,
                    github_token=github_token,
                    dispatch_token=dispatch_token,
                    repo=repo,
                    ref=ref,
                )
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
    allowed_chats = _parse_allowed_chats(os.environ.get("TELEGRAM_ALLOWED_CHATS", ""))
    github_token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    dispatch_token = os.environ.get("GH_DISPATCH_TOKEN", "").strip() or None
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip() or None
    ref = os.environ.get("GITHUB_REF_NAME", "main").strip() or "main"

    if not token:
        log.warning("TELEGRAM_BOT_TOKEN missing — exiting cleanly (skip)")
        return 0

    return run(
        token,
        dashboard_url,
        run_seconds=run_seconds,
        poll_timeout=poll_timeout,
        allowed_chats=allowed_chats,
        github_token=github_token,
        dispatch_token=dispatch_token,
        repo=repo,
        ref=ref,
    )


if __name__ == "__main__":
    sys.exit(main())
