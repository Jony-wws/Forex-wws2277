"""AI narrative generator — writes a 1-paragraph market explanation.

Reads the latest 5h cycle report and asks GitHub Models (free) to
produce a short human-friendly narrative for Telegram and the dashboard:

> «Сегодня EUR/USD в восходящем тренде из-за слабости доллара после
>  CPI; в топе ★ ПРЕМИУМ — GBPCHF, persistence 100 %.  Не входить в
>  AUDUSD — пара деградировала, WR 25 % за последнюю неделю».

This is the "complex AI" piece the user asked for — it actually writes
in Russian and explains *why*, not just numbers.

Output:
- ``reports/ai_narrative_latest.md`` — narrative text.
- Telegram message if ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` set.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)

CYCLE_REPORT = REPORTS / "cycle_5h_latest.md"
DEGRADATION = REPORTS / "degradation_latest.md"
OUT = REPORTS / "ai_narrative_latest.md"

SYSTEM_PROMPT = """\
Ты — старший форекс-аналитик в трейдинговой системе. Тебе дают
выдержку из 5-часового цикла (топ-пары по текущему рынку) и список
деградировавших пар.  Напиши **один абзац** на русском, 4-6 предложений:

- Что произошло на рынке за последние 5 часов (по топ-парам).
- Какие пары уверенно идут в одну сторону и почему (опираясь на
  multi-TF, persistence, ADX из отчёта).
- Какие пары лучше пропустить (из деградировавших).
- В конце — короткая рекомендация: «фокус на …», «избегать …».

Никаких длинных объяснений теории, никакого markdown. Просто читаемый
русский текст для трейдера, который смотрит сообщение на Android.
"""


def read_text(path: Path, limit: int = 6000) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except FileNotFoundError:
        return ""


def call_cloudflare_workers_ai(
    prompt: str,
    account_id: str,
    api_token: str,
    model: str = "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
) -> str | None:
    """Cloudflare Workers AI — primary narrator (Llama 3.3 70B, free tier).

    Endpoint: ``https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}``
    Auth:     ``Authorization: Bearer {api_token}``
    Docs:     https://developers.cloudflare.com/workers-ai/
    """
    if not (account_id and api_token):
        return None
    body = json.dumps({
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 600,
        "temperature": 0.4,
    }).encode("utf-8")
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/ai/run/{model}"
    )
    req = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_token}",
            "content-type": "application/json",
            "accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"[ai_narrative] cloudflare workers ai failed: {e}", file=sys.stderr)
        return None
    if not data.get("success"):
        errs = data.get("errors") or []
        print(f"[ai_narrative] cloudflare returned errors: {errs}", file=sys.stderr)
        return None
    result = data.get("result") or {}
    text = result.get("response")
    if isinstance(text, str) and text.strip():
        return text
    choices = result.get("choices") or []
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        if isinstance(msg.get("content"), str):
            return msg["content"]
    return None


def call_github_models(prompt: str, token: str) -> str | None:
    model = os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 600,
        "temperature": 0.4,
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
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return (data.get("choices", [{}])[0].get("message", {}) or {}).get("content")
    except Exception as e:
        print(f"[ai_narrative] github models failed: {e}", file=sys.stderr)
        return None


def telegram_send(text: str) -> None:
    bot = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (bot and chat):
        return
    try:
        urlopen(Request(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            data=urlencode({"chat_id": chat, "text": text[:3500]}).encode(),
        ), timeout=10)
    except Exception as e:
        print(f"[ai_narrative] telegram failed: {e}", file=sys.stderr)


def main() -> int:
    cycle_md = read_text(CYCLE_REPORT)
    degraded_md = read_text(DEGRADATION, limit=1500)
    if not cycle_md:
        OUT.write_text("# AI narrative — нет cycle_5h_latest.md\n", encoding="utf-8")
        return 0

    prompt = f"## Цикл 5h\n{cycle_md}\n\n## Деградировавшие\n{degraded_md or '(нет)'}"

    cf_account = os.getenv("CF_AI_ACCOUNT_ID")
    cf_token = os.getenv("CF_AI_API_TOKEN")
    cf_model = os.getenv("CF_AI_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    gh_token = os.getenv("GITHUB_TOKEN")

    narrative: str | None = None
    # 1. Cloudflare Workers AI — PRIMARY (free Llama 3.3 70B).
    if cf_account and cf_token:
        narrative = call_cloudflare_workers_ai(
            prompt, cf_account, cf_token, cf_model,
        )
    # 2. GitHub Models — transparent fallback when CF secrets missing/fail.
    if not narrative and gh_token:
        narrative = call_github_models(prompt, gh_token)
    if not narrative:
        narrative = "(LLM недоступен — повторим на следующем цикле)"

    OUT.write_text(
        f"# AI narrative — {os.getenv('GITHUB_RUN_ID','local')}\n\n"
        f"{narrative.strip()}\n",
        encoding="utf-8",
    )
    print(narrative.strip()[:500])
    telegram_send(f"📊 FOREX-обзор:\n{narrative.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
