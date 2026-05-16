"""Telegram → GitHub Actions agent.

Принимает свободный текст пользователя из Telegram, через **бесплатные**
GitHub Models (``GITHUB_TOKEN`` → ``models.github.ai``) превращает его в
структурированное намерение и дёргает соответствующую GitHub Actions
workflow через REST API ``POST /repos/{owner}/{repo}/actions/workflows/
{wf}/dispatches``.

Дизайн:

* **Whitelist tools.** Список разрешённых workflows жёстко закодирован
  в :data:`WORKFLOWS`. LLM никогда не пишет код и не дёргает чужие
  workflows — только выбирает имя из whitelist + проставляет inputs.
* **Безопасные дефолты.** При любой ошибке парсинга/API возвращаем
  человекочитаемое сообщение, не падаем.
* **Stateless.** Один вызов = один intent. История не хранится — она и
  не нужна для запуска workflow.
* **Бесплатно.** Используем тот же ``GITHUB_TOKEN`` что доступен в любом
  workflow + GitHub Models free tier ``openai/gpt-4o-mini``.

Минимальное использование::

    from telegram_agent import parse_and_dispatch

    result = parse_and_dispatch(
        user_text="запусти 5-часовой цикл",
        github_token=os.environ["GITHUB_TOKEN"],
        dispatch_token=os.environ["GH_DISPATCH_TOKEN"],
        repo="Jony-wws/Forex-wws2277",
        ref="main",
    )
    print(result["reply"])         # текст для пользователя
    print(result["dispatched"])    # True/False — действительно дёрнули workflow

Module also exposes :func:`parse_intent` and :func:`dispatch_workflow`
независимо — удобно для тестов и для случаев, когда дёргать workflow
не нужно (например, бот отвечает на «привет» текстом).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("telegram_agent")

# ── Whitelist of dispatchable workflows ──────────────────────────────
#
# Each entry maps a *short, stable* name (used by the LLM) to the actual
# workflow file under ``.github/workflows/``.  ``inputs`` is a schema
# the LLM is asked to fill — keys are input names, values are short
# descriptions.  If a workflow has no inputs, leave ``inputs`` empty.
#
# **Adding a new tool:** just append an entry here AND make sure the
# corresponding workflow has ``on: workflow_dispatch:`` so the REST API
# can trigger it.  No other code changes needed.

WORKFLOWS: dict[str, dict[str, Any]] = {
    "cycle_5h": {
        "file": "cycle_5h.yml",
        "description": (
            "5-часовой адаптивный цикл по всем 28 парам: загрузка свечей, "
            "sweep сетки параметров, бэктест, топ-3 + полный отчёт в Telegram. "
            "Запускать когда пользователь просит «свежий цикл», «топ-3», "
            "«перепрогнать стратегию», «обнови сигналы», и т.п."
        ),
        "inputs": {},
    },
    "site_screenshot": {
        "file": "site_screenshot.yml",
        "description": (
            "Скриншот публичного дашборда (SITE_URL из секретов) через "
            "Playwright + Chromium. Запускать когда просят «скриншот сайта», "
            "«покажи как сейчас выглядит дашборд»."
        ),
        "inputs": {},
    },
    "tv_screenshot": {
        "file": "tv_screenshot.yml",
        "description": (
            "Скриншот графика TradingView для конкретной пары. Запускать "
            "когда просят «скриншот EURUSD на TradingView», «график GBPJPY»."
        ),
        "inputs": {},
    },
    "backtest": {
        "file": "backtest.yml",
        "description": (
            "Бэктест EURUSD на исторических данных. Запускать когда просят "
            "«бэктест», «прогон по истории», «проверь стратегию на год»."
        ),
        "inputs": {},
    },
    "multi_tf_backtest": {
        "file": "multi_tf_backtest.yml",
        "description": (
            "Мульти-таймфрейм бэктест по всем 28 парам (D1/H4/H1/M15). "
            "Запускать когда просят «полный бэктест 28 пар»."
        ),
        "inputs": {},
    },
    "ai_review": {
        "file": "ai_review.yml",
        "description": (
            "AI-обзор последнего цикла: LLM читает отчёт и предлагает "
            "изменения параметров. Запускать когда просят «что улучшить?», "
            "«AI-обзор»."
        ),
        "inputs": {},
    },
    "ai_narrative": {
        "file": "ai_narrative.yml",
        "description": (
            "AI-сводка рынка для трейдера на человеческом языке. Запускать "
            "когда просят «расскажи что на рынке», «narrative», «обзор рынка»."
        ),
        "inputs": {},
    },
    "ai_patcher": {
        "file": "ai_patcher.yml",
        "description": (
            "AI-патчер: ⚠ опасно — LLM меняет код. Запускать ТОЛЬКО когда "
            "пользователь явно просит «AI-патч», «правь код», «сделай PR с "
            "правкой». В сомнении выбирать ai_review, а не ai_patcher."
        ),
        "inputs": {},
    },
    "weekly_report": {
        "file": "weekly_report.yml",
        "description": (
            "Недельный отчёт по WR и стратегиям. Запускать когда просят "
            "«отчёт за неделю», «как было на неделе»."
        ),
        "inputs": {},
    },
    "health_check": {
        "file": "health_check.yml",
        "description": (
            "Проверка здоровья API/дашборда. Запускать когда просят «работает "
            "ли сайт», «health»."
        ),
        "inputs": {},
    },
    "multi_broker": {
        "file": "multi_broker.yml",
        "description": (
            "Сравнение цен брокеров. Запускать когда просят «brokers», "
            "«цены брокеров», «спред»."
        ),
        "inputs": {},
    },
    "drift_detector": {
        "file": "drift_detector.yml",
        "description": (
            "Детектор дрейфа стратегии. Запускать когда просят «дрифт», "
            "«падает ли стратегия»."
        ),
        "inputs": {},
    },
    "news_watcher": {
        "file": "news_watcher.yml",
        "description": (
            "Парсер новостей форекса. Запускать когда просят «новости», "
            "«что нового на рынке»."
        ),
        "inputs": {},
    },
}

# Special intent kinds that don't dispatch any workflow.
INTENT_REPLY_ONLY = "reply"
INTENT_HELP = "help"
INTENT_UNKNOWN = "unknown"
INTENT_DISPATCH = "dispatch"


# ── GitHub Models (LLM intent parsing) ───────────────────────────────


def _build_system_prompt() -> str:
    """System prompt with the live tool whitelist injected."""
    tools = []
    for name, spec in WORKFLOWS.items():
        inputs_str = (
            ", ".join(f"{k} ({v})" for k, v in spec["inputs"].items())
            or "нет"
        )
        tools.append(
            f"- **{name}** — {spec['description']} Inputs: {inputs_str}."
        )
    tools_block = "\n".join(tools)
    return f"""Ты — интент-парсер для Telegram-агента FOREX-системы.
Пользователь пишет тебе свободным текстом по-русски. Твоя задача —
выбрать действие и вернуть СТРОГО валидный JSON. Никакого markdown,
никаких пояснений вокруг JSON.

Возможные действия (поле "action"):
- "dispatch" — запустить workflow. Заполни "workflow" (одно из имён
  ниже) и "inputs" (объект, может быть пустым).
- "reply" — обычный разговор / приветствие / уточнение. Никакого
  workflow не дёргаем, только "reply" с человекочитаемым ответом.
- "help" — показать список доступных команд.
- "unknown" — не понял запрос. Заполни "reply" с уточнением.

Доступные workflows:
{tools_block}

ОБЯЗАТЕЛЬНЫЕ поля JSON:
- "action": одно из dispatch | reply | help | unknown
- "reply": короткий русский текст для пользователя (≤200 символов)

Если "action" == "dispatch":
- "workflow": ИМЯ из списка выше (НЕ путь к файлу)
- "inputs": объект (может быть {{}})

Пример dispatch:
{{"action":"dispatch","workflow":"cycle_5h","inputs":{{}},"reply":"Запускаю 5-часовой цикл. Отчёт придёт через ~3 мин."}}

Пример reply:
{{"action":"reply","inputs":{{}},"reply":"Привет! Чем помочь? Напиши /help чтобы увидеть команды."}}

Пример unknown:
{{"action":"unknown","inputs":{{}},"reply":"Не понял. Уточни — нужен цикл, скриншот, бэктест?"}}

ВАЖНО: возвращай ТОЛЬКО JSON, без префиксов вроде ```json и без
комментариев."""


def _call_github_models(
    user_text: str,
    token: str,
    model: str | None = None,
    timeout: int = 30,
) -> str | None:
    """Call GitHub Models inference endpoint. Returns raw assistant text or None."""
    if not token:
        return None
    model = model or os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 400,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = Request(
        "https://models.github.ai/inference/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    except (HTTPError, URLError) as e:
        log.warning("github models call failed: %s", e)
        return None
    except Exception as e:  # pragma: no cover - defensive
        log.warning("github models unexpected error: %s", e)
        return None


# ── Heuristic fallback (no LLM available) ────────────────────────────


_HEURISTICS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(цикл|sweep|top-?3|топ-?3|пересчита|перепрогн|свеж\w* сигнал)", re.I), "cycle_5h"),
    (re.compile(r"\b(скриншот|снимок).{0,40}(сайт|дашборд|dashboard)", re.I), "site_screenshot"),
    (re.compile(r"\b(скриншот|график).{0,40}(tradingview|tv|пар[уы]|[A-Z]{3}[A-Z]{3})", re.I), "tv_screenshot"),
    (re.compile(r"\b(бэктест|backtest).{0,40}(28|мульти|all|вс[её])", re.I), "multi_tf_backtest"),
    (re.compile(r"\b(бэктест|backtest)\b", re.I), "backtest"),
    (re.compile(r"\b(ai[\s-]?review|обзор|улучш\w*|анализ\w* цикл)", re.I), "ai_review"),
    (re.compile(r"\b(narrative|сводк|обзор\w* рынк|расскажи\w* рынк)", re.I), "ai_narrative"),
    (re.compile(r"\b(week|недел\w*)\b.{0,20}(отчёт|report)", re.I), "weekly_report"),
    (re.compile(r"\b(health|жив\w*|работает\w* сайт|здоров)", re.I), "health_check"),
    (re.compile(r"\b(broker|брокер|спред|spread)", re.I), "multi_broker"),
    (re.compile(r"\b(дрифт|drift)", re.I), "drift_detector"),
    (re.compile(r"\b(новост|news)\b", re.I), "news_watcher"),
]


def _heuristic_intent(user_text: str) -> dict[str, Any]:
    text = (user_text or "").strip()
    low = text.lower()
    if not text or low in {"/start", "/help", "?", "помощь", "help"}:
        return {"action": INTENT_HELP, "inputs": {}, "reply": _help_text()}
    if any(low.startswith(g) for g in ("привет", "хай", "hi", "hello", "yo")):
        return {
            "action": INTENT_REPLY_ONLY,
            "inputs": {},
            "reply": "Привет! Напиши, что нужно (например «свежий цикл» или «скриншот сайта»), или /help.",
        }
    for pattern, wf in _HEURISTICS:
        if pattern.search(text):
            spec = WORKFLOWS[wf]
            return {
                "action": INTENT_DISPATCH,
                "workflow": wf,
                "inputs": {},
                "reply": f"Запускаю {wf}. {spec['description'].split('.')[0]}.",
            }
    return {
        "action": INTENT_UNKNOWN,
        "inputs": {},
        "reply": (
            "Не понял. Попробуй: «свежий цикл», «скриншот сайта», "
            "«бэктест 28 пар», «AI-обзор», «health». Или /help."
        ),
    }


def _help_text() -> str:
    lines = ["Что я умею (любой свободный текст или явная команда):"]
    for name, spec in WORKFLOWS.items():
        first_sentence = spec["description"].split(".")[0]
        lines.append(f"• /{name} — {first_sentence}")
    lines.append("")
    lines.append("Просто напиши, чего хочешь — я пойму без команд.")
    return "\n".join(lines)


# ── Public: intent parsing ──────────────────────────────────────────


def parse_intent(user_text: str, github_token: str | None = None) -> dict[str, Any]:
    """Превратить свободный текст в структурированный intent.

    Возвращаемый словарь всегда содержит ключи:

    * ``action``: ``"dispatch"`` | ``"reply"`` | ``"help"`` | ``"unknown"``
    * ``reply``: строка для отправки в Telegram
    * ``inputs``: dict (пустой если не нужно)

    Если ``action == "dispatch"``, дополнительно есть:

    * ``workflow``: ключ из :data:`WORKFLOWS`

    Если LLM недоступен (нет токена / API упало), скатываемся в
    эвристический парсер — он покрывает базовые сценарии и никогда не
    падает.
    """
    text = (user_text or "").strip()
    if not text:
        return {"action": INTENT_HELP, "inputs": {}, "reply": _help_text()}

    # Explicit slash command — bypass LLM, just look up the whitelist.
    if text.startswith("/"):
        cmd = text.lstrip("/").split()[0].lower()
        if cmd in {"help", "start"}:
            return {"action": INTENT_HELP, "inputs": {}, "reply": _help_text()}
        if cmd in WORKFLOWS:
            spec = WORKFLOWS[cmd]
            return {
                "action": INTENT_DISPATCH,
                "workflow": cmd,
                "inputs": {},
                "reply": f"Запускаю /{cmd} — {spec['description'].split('.')[0]}.",
            }
        # unknown slash command falls through to LLM (it may still understand)

    raw = _call_github_models(text, github_token) if github_token else None
    if raw:
        parsed = _coerce_llm_json(raw)
        if parsed is not None:
            return parsed

    return _heuristic_intent(text)


def _coerce_llm_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from LLM output.

    Handles a few common failure modes:
    * fenced ```json ... ``` blocks
    * trailing prose after the JSON object
    * model returning a plain string by mistake (we wrap it as reply)
    """
    s = raw.strip()
    # strip optional code fence
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S)
    if fence:
        s = fence.group(1).strip()
    # extract the first JSON object if there's trailing prose
    if not s.startswith("{"):
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            s = m.group(0)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    action = obj.get("action")
    reply = str(obj.get("reply") or "").strip()
    inputs = obj.get("inputs") or {}
    if not isinstance(inputs, dict):
        inputs = {}
    if action == INTENT_DISPATCH:
        wf = obj.get("workflow")
        if not isinstance(wf, str) or wf not in WORKFLOWS:
            # LLM hallucinated a workflow → degrade to unknown.
            return {
                "action": INTENT_UNKNOWN,
                "inputs": {},
                "reply": reply or "Не нашёл подходящую команду. Попробуй /help.",
            }
        return {
            "action": INTENT_DISPATCH,
            "workflow": wf,
            "inputs": inputs,
            "reply": reply or f"Запускаю {wf}…",
        }
    if action in (INTENT_REPLY_ONLY, INTENT_HELP, INTENT_UNKNOWN):
        if action == INTENT_HELP:
            return {"action": INTENT_HELP, "inputs": {}, "reply": _help_text()}
        return {
            "action": action,
            "inputs": {},
            "reply": reply or "…",
        }
    # Unknown action verb → treat as plain reply.
    return {
        "action": INTENT_REPLY_ONLY,
        "inputs": {},
        "reply": reply or "Не понял запрос. /help",
    }


# ── Public: workflow dispatch ────────────────────────────────────────


class DispatchError(RuntimeError):
    """Raised when the GitHub Actions REST call fails."""


def dispatch_workflow(
    repo: str,
    workflow_file: str,
    ref: str,
    inputs: dict[str, Any],
    pat: str,
    timeout: int = 20,
) -> None:
    """POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches.

    Raises :class:`DispatchError` on any HTTP or transport error so the
    caller can surface a precise reason to the user.
    """
    if not pat:
        raise DispatchError("отсутствует GH_DISPATCH_TOKEN — секрет не задан")
    if "/" not in repo:
        raise DispatchError(f"некорректный repo: {repo!r}")
    body = json.dumps({"ref": ref, "inputs": inputs or {}}).encode("utf-8")
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{workflow_file}/dispatches"
    )
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "Forex-wws2277-telegram-agent",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            # Successful dispatch returns 204 No Content.
            if resp.status not in (200, 201, 204):
                raise DispatchError(f"unexpected status {resp.status}")
    except HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")[:300] if hasattr(e, "read") else ""
        raise DispatchError(f"HTTP {e.code}: {msg}") from e
    except URLError as e:
        raise DispatchError(f"network error: {e.reason}") from e


# ── Public: combined parse + dispatch ────────────────────────────────


def parse_and_dispatch(
    user_text: str,
    *,
    github_token: str | None = None,
    dispatch_token: str | None = None,
    repo: str | None = None,
    ref: str = "main",
) -> dict[str, Any]:
    """Полный цикл: парсим intent + (если нужно) дёргаем workflow.

    Возвращает словарь с теми же ключами что и :func:`parse_intent`
    плюс:

    * ``dispatched``: ``True`` если успешно дёрнули workflow,
      иначе ``False``
    * ``run_url`` (опционально): ссылка на список запусков workflow
    * ``error`` (опционально): человекочитаемая ошибка
    """
    intent = parse_intent(user_text, github_token=github_token)
    result: dict[str, Any] = dict(intent)
    result["dispatched"] = False

    if intent["action"] != INTENT_DISPATCH:
        return result

    wf_name = intent["workflow"]
    spec = WORKFLOWS[wf_name]
    if not repo or not dispatch_token:
        result["error"] = (
            "Не могу запустить workflow: не задан GH_DISPATCH_TOKEN или repo. "
            "Запрос распознан как: " + wf_name
        )
        return result

    try:
        dispatch_workflow(
            repo=repo,
            workflow_file=spec["file"],
            ref=ref,
            inputs=intent.get("inputs") or {},
            pat=dispatch_token,
        )
    except DispatchError as e:
        result["error"] = f"GitHub API: {e}"
        return result

    result["dispatched"] = True
    result["run_url"] = f"https://github.com/{repo}/actions/workflows/{spec['file']}"
    return result


# ── Manual CLI entrypoint (для отладки) ──────────────────────────────


def main() -> int:
    """``python scripts/telegram_agent.py "запусти цикл"`` — для отладки."""
    if len(sys.argv) < 2:
        print("usage: telegram_agent.py <user text>")
        return 1
    text = " ".join(sys.argv[1:])
    gh = os.environ.get("GITHUB_TOKEN", "").strip() or None
    pat = os.environ.get("GH_DISPATCH_TOKEN", "").strip() or None
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip() or None
    ref = os.environ.get("GITHUB_REF_NAME", "main").strip() or "main"
    res = parse_and_dispatch(
        text,
        github_token=gh,
        dispatch_token=pat,
        repo=repo,
        ref=ref,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
