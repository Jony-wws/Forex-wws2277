"""AI strategy reviewer — runs after each 5h cycle on GitHub Actions.

Acts as a "Devin-level" automated forex-strategy expert that reads the
latest cycle outputs and produces a critical review with concrete,
actionable parameter suggestions for ``app/cycle.py`` and
``scripts/cycle_5h.py``.

Three review modes, in priority order, ALL FREE:

1. **GitHub Models** (preferred, FREE) — uses the ``GITHUB_TOKEN`` that
   GitHub Actions already injects into every workflow run.  No paid API
   key needed.  Endpoint: ``https://models.github.ai/inference``.
   Default model: ``openai/gpt-4o-mini`` (free tier ~50 req/day, plenty
   for our 5 cycles/day).  Override with the ``GITHUB_MODEL`` env var.

2. **HuggingFace Inference** (FREE with HF token) — backup when the
   GitHub Models quota is exhausted.  Set ``HF_TOKEN`` to enable.

3. **Heuristic mode** (always works, no key needed) — deterministic
   rule-based reviewer:
   - Reads the rolling 5h winrate and the 28-pair backtest WR.
   - Flags pairs whose WR fell ≥10pp over 3 consecutive cycles.
   - Suggests raising ``STRONG_CONFIDENCE``, ``STRONG_RATIO`` or
     ``STRONG_PERSISTENCE`` when WR < 60 % across the last 10 cycles.
   - Suggests dropping pairs that consistently lose.

Anthropic / OpenAI paid keys are still honoured if present, but they are
NOT required and NOT recommended — keep things free.

Outputs:
- ``reports/ai_review_latest.md`` — full review text.
- Optional Telegram message via ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
STATE = ROOT / "state"
REPORTS.mkdir(exist_ok=True)

CYCLE_REPORT = REPORTS / "cycle_5h_latest.md"
BACKTEST_REPORT = REPORTS / "eurusd_backtest_latest.md"
DEGRADATION_REPORT = REPORTS / "degradation_fix.md"
MEMORY_REPORT = REPORTS / "memory_neighbors_latest.md"
OUTPUT = REPORTS / "ai_review_latest.md"

CONFIG_TARGETS = {
    "app/cycle.py": [
        "STRONG_CONFIDENCE", "STRONG_RATIO", "STRONG_ADX_H1",
        "STRONG_ADX_H4", "STRONG_PERSISTENCE",
        "PREMIUM_ADX_H1", "PREMIUM_PERSISTENCE",
        "MIN_PICKS", "MAX_PICKS",
    ],
    "scripts/cycle_5h.py": [
        "MIN_TRADES_PER_DAY", "TOP_N",
    ],
}


# ── input collection ───────────────────────────────────────────────────


def read_text(path: Path, max_bytes: int = 20_000) -> str:
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="ignore")
    return data[:max_bytes]


def parse_winrate(text: str) -> tuple[float | None, int | None]:
    """Pull the headline 5h WR + decisions count out of cycle_5h_latest.md."""
    m = re.search(r"WR за 5 часов = \*\*([0-9.]+)%\*\*", text)
    wr = float(m.group(1)) if m else None
    m2 = re.search(
        r"попаданий: \*\*(\d+)\*\*\s*·\s*промахов: \*\*(\d+)\*\*", text
    )
    decisions = int(m2.group(1)) + int(m2.group(2)) if m2 else None
    return wr, decisions


def parse_28pair_wr(text: str) -> float | None:
    m = re.search(r"\*\*Premium\*\*[^|]*\|\s*\d+\s*\|\s*([0-9.]+)\s*%", text)
    if m:
        return float(m.group(1))
    m = re.search(r"Win Rate\s*\|\s*\*\*([0-9.]+)\s*%", text)
    return float(m.group(1)) if m else None


def parse_degraded_pairs(text: str) -> list[str]:
    return re.findall(r"\*\*([A-Z]{6})\*\*: WR", text or "")


# ── heuristic reviewer ────────────────────────────────────────────────


def heuristic_review() -> str:
    cycle_text = read_text(CYCLE_REPORT)
    backtest_text = read_text(BACKTEST_REPORT, 8_000)
    degraded_text = read_text(DEGRADATION_REPORT, 4_000)

    wr_5h, decisions = parse_winrate(cycle_text)
    wr_28 = parse_28pair_wr(backtest_text)
    degraded = parse_degraded_pairs(degraded_text)

    lines: list[str] = []
    lines.append("## 🧠 Эвристический AI-обзор (без LLM)")
    lines.append("")
    lines.append("### Метрики")
    lines.append(
        f"- **WR за последние 5 часов:** {wr_5h if wr_5h is not None else '—'}%"
        f" ({decisions or 0} решений)"
    )
    lines.append(
        f"- **WR на 28-парном бэктесте:** {wr_28 if wr_28 is not None else '—'}%"
    )
    lines.append(
        f"- **Деградировавшие пары:** {', '.join(degraded) if degraded else 'нет'}"
    )
    lines.append("")
    lines.append("### Диагноз")

    suggestions: list[str] = []
    if wr_5h is not None and wr_5h < 50:
        lines.append(
            f"- ⚠ WR {wr_5h:.1f}% сильно ниже break-even (55.6% для 80% binary)."
        )
        suggestions.append(
            "Поднять `STRONG_CONFIDENCE` 88 → 90, `STRONG_RATIO` 0.55 → 0.60, "
            "`STRONG_PERSISTENCE` 80 → 100 (требовать все 5 баров за 5ч в одну сторону)."
        )
    elif wr_5h is not None and wr_5h < 60:
        lines.append(f"- WR {wr_5h:.1f}% — около break-even, есть запас для жёсткости.")
        suggestions.append(
            "Поднять `STRONG_ADX_H1` 25 → 28, `STRONG_ADX_H4` 20 → 22."
        )
    else:
        lines.append("- WR в норме — текущая жёсткость работает.")

    if wr_28 is not None and wr_28 < 50:
        lines.append(f"- ⚠ 28-парный бэктест {wr_28:.1f}% — система убыточна на длинной истории.")
        suggestions.append(
            "В `scripts/cycle_5h.py` поднять `MIN_TRADES_PER_DAY` 3 → 5 чтобы отсечь "
            "редкие пары, и сузить sweep grid (только тренд, без откатов)."
        )

    if degraded:
        lines.append(
            f"- Пары в деградации ({len(degraded)}): "
            f"{', '.join(f'`{p}`' for p in degraded)} — рассмотреть исключение из топа на 24ч."
        )
        suggestions.append(
            "В `app/cycle.py._select_strict` добавить временный blacklist "
            f"для деградировавших пар: {degraded}."
        )

    if not suggestions:
        suggestions.append(
            "Метрики в норме — ничего трогать не нужно. "
            "Продолжить мониторинг."
        )

    lines.append("")
    lines.append("### Рекомендации к параметрам")
    for s in suggestions:
        lines.append(f"- {s}")

    lines.append("")
    lines.append("### Точки правки")
    for path, names in CONFIG_TARGETS.items():
        lines.append(f"- `{path}`: {', '.join(f'`{n}`' for n in names)}")

    return "\n".join(lines)


# ── LLM reviewer (Anthropic / OpenAI) ─────────────────────────────────


SYSTEM_PROMPT = """Ты — эксперт по торговле на форексе и квант-аналитик уровня Devin.
Твоя задача — критически разобрать отчёт о 5-часовом цикле системы FOREX
Сигналы 2026 (28 валютных пар, бинарные опционы 80%, M15/H1/H4/D1 анализ)
и выдать **конкретные, измеримые** рекомендации для ужесточения фильтров.

Цель пользователя: минимизировать минусы, оставить только пары с явным
трендом ≥5 часов, иметь минимум 3 сильных тренда каждые 5 часов.

Текущие пороги STRONG-тира в app/cycle.py:
- STRONG_CONFIDENCE = 88
- STRONG_RATIO = 0.55
- STRONG_ADX_H1 = 25
- STRONG_ADX_H4 = 20
- STRONG_PERSISTENCE = 80 (≥4 из 5 H1-баров в сторону тренда)

Формат ответа — Markdown, на русском языке. Секции:
1. **Диагноз** — что не работает в этом цикле, в 2-3 пунктах
2. **Рекомендации** — конкретные значения порогов (3-5 штук)
3. **Что НЕ менять** — параметры, которые трогать не нужно
4. **Риски** — побочные эффекты предлагаемых изменений
"""


def call_github_models(prompt: str, token: str) -> str | None:
    """Use GitHub Models — FREE, uses GITHUB_TOKEN that Actions already provides.

    Endpoint docs: https://docs.github.com/en/github-models
    Free tier: roughly 50 requests/day, more than enough for 5 cycles/day.
    """
    model = os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1500,
        "temperature": 0.2,
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
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[ai_review] github models call failed: {e}", file=sys.stderr)
        return None


def call_huggingface(prompt: str, token: str) -> str | None:
    """HuggingFace Inference API — also free with a HF token."""
    model = os.getenv(
        "HF_MODEL", "meta-llama/Llama-3.3-70B-Instruct"
    )
    body = json.dumps({
        "inputs": f"{SYSTEM_PROMPT}\n\nЗадание:\n{prompt}",
        "parameters": {"max_new_tokens": 1500, "temperature": 0.2},
    }).encode("utf-8")
    req = Request(
        f"https://api-inference.huggingface.co/models/{model}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list) and data:
            return data[0].get("generated_text", "").strip()
        return None
    except Exception as e:
        print(f"[ai_review] hf call failed: {e}", file=sys.stderr)
        return None


def call_anthropic(prompt: str, api_key: str) -> str | None:
    body = json.dumps({
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 1500,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        chunks = data.get("content", [])
        return "".join(c.get("text", "") for c in chunks if c.get("type") == "text")
    except Exception as e:
        print(f"[ai_review] anthropic call failed: {e}", file=sys.stderr)
        return None


def call_openai(prompt: str, api_key: str) -> str | None:
    body = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1500,
    }).encode("utf-8")
    req = Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[ai_review] openai call failed: {e}", file=sys.stderr)
        return None


def llm_review() -> str | None:
    cycle_text = read_text(CYCLE_REPORT)
    backtest_text = read_text(BACKTEST_REPORT, 8_000)
    degraded_text = read_text(DEGRADATION_REPORT, 4_000)
    memory_text = read_text(MEMORY_REPORT, 6_000)

    prompt_parts = ["# Отчёт 5-часового цикла", cycle_text]
    if backtest_text:
        prompt_parts += ["\n\n# 28-парный бэктест (выборка)", backtest_text]
    if degraded_text:
        prompt_parts += ["\n\n# Деградировавшие стратегии", degraded_text]
    if memory_text:
        prompt_parts += ["\n\n# Память аналогов (Supabase pgvector)", memory_text]
    prompt = "\n".join(prompt_parts)[:18_000]

    # 1. GitHub Models — FREE, no extra secrets required.  GITHUB_TOKEN is
    # auto-injected into every Actions run, so this is the default path.
    gh_token = os.getenv("GITHUB_TOKEN")
    if gh_token:
        out = call_github_models(prompt, gh_token)
        if out:
            model_label = os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
            return f"## 🧠 AI-обзор (GitHub Models · {model_label} · бесплатно)\n\n{out.strip()}"

    # 2. HuggingFace Inference — also free with a HF token.
    hf = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
    if hf:
        out = call_huggingface(prompt, hf)
        if out:
            model_label = os.getenv("HF_MODEL", "meta-llama/Llama-3.3-70B-Instruct")
            return f"## 🧠 AI-обзор (HuggingFace · {model_label} · бесплатно)\n\n{out.strip()}"

    # 3-4. Optional paid fallbacks — only used if explicit keys are set.
    anth = os.getenv("ANTHROPIC_API_KEY")
    if anth:
        out = call_anthropic(prompt, anth)
        if out:
            return f"## 🧠 AI-обзор (Claude Sonnet 4.5)\n\n{out.strip()}"
    oai = os.getenv("OPENAI_API_KEY")
    if oai:
        out = call_openai(prompt, oai)
        if out:
            return f"## 🧠 AI-обзор (GPT-4o mini)\n\n{out.strip()}"
    return None


# ── telegram delivery ─────────────────────────────────────────────────


def telegram_send(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    body = urlencode({
        "chat_id": chat,
        "text": text[:3500],
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
    )
    try:
        urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"[ai_review] telegram send failed: {e}", file=sys.stderr)


# ── main ──────────────────────────────────────────────────────────────


def main() -> int:
    sections: list[str] = ["# 🤖 AI-обзор стратегии — авто-генерация"]
    sections.append(
        "_Этот файл создаётся GitHub Actions workflow `ai_review.yml` "
        "каждый раз после завершения 5-часового цикла._"
    )

    llm = llm_review()
    if llm:
        sections.append(llm)
    sections.append(heuristic_review())

    text = "\n\n".join(sections) + "\n"
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"[ai_review] wrote {OUTPUT} ({len(text)} chars)")

    telegram_send(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
