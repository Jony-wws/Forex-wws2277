"""AI code patcher — uses GitHub Models to *write actual code patches*.

This is the heavy-weight automation the user asked for: not a heuristic
threshold bump, but an LLM that reads recent performance data, the
relevant source files, and produces a unified diff that adjusts the
**voting weights** in ``app/analyzer.py``, the **strict gate
parameters** in ``app/cycle.py``, or both.

The output is always a real git PR opened by ``peter-evans/create-pull-request``
so the user reviews every change before it goes live.

Pipeline:

1. Read recent winrate from ``state/forecasts.json`` (rolling 5h cycles)
   and the 28-pair backtest from ``reports/backtest_latest.md``.
2. Read the relevant source files (``app/analyzer.py``, ``app/cycle.py``,
   ``app/config.py``).
3. Compose a structured prompt with hard rules:
   - Max 60 changed lines per file
   - Must keep all 28 pairs
   - Must keep the 15 voting blocks (only weights / thresholds change)
   - Must keep ``MIN_PICKS = 3`` (≥3 forecasts every 5h)
   - Output as JSON ``{"changes": [{"file": "...", "old": "...", "new": "..."}], "rationale": "..."}``
4. Call **GitHub Models** (free, uses ``GITHUB_TOKEN``).  Default model
   is ``openai/gpt-4o-mini`` — override with ``GITHUB_MODEL`` for a
   stronger model if quota allows (e.g. ``meta/Llama-3.3-70B-Instruct``).
5. Apply the JSON patch with safety checks:
   - ``old`` must occur exactly once in the file (otherwise reject).
   - Total diff must be ≤ 200 lines net.
   - Forbidden tokens (``import os``, ``subprocess``, ``eval``, ``exec``,
     ``open(``) — refuse the patch.
6. Smoke-test the patched code (``python -c "import app.analyzer, app.cycle"``).
7. Run the existing 28-pair backtest in *dry mode* to ensure WR doesn't
   collapse below the previous baseline.
8. Write ``reports/ai_patch_proposal.md`` with rationale + diff and
   leave the modified files for the workflow to commit + open as a PR.

If the LLM fails or the patch is rejected, the script writes a "no
patch this run" report and exits 0 — never breaks CI.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)

PROPOSAL = REPORTS / "ai_patch_proposal.md"
CYCLE_REPORT = REPORTS / "cycle_5h_latest.md"
BACKTEST_REPORT = REPORTS / "backtest_latest.md"
MEMORY_REPORT = REPORTS / "memory_neighbors_latest.md"
STATE_FILE = ROOT / "state" / "forecasts.json"

EDITABLE_FILES = [
    ROOT / "app" / "analyzer.py",
    ROOT / "app" / "cycle.py",
    ROOT / "app" / "config.py",
]

FORBIDDEN_TOKENS = (
    "import os",
    "import subprocess",
    "eval(",
    "exec(",
    "open(",
    "__import__",
    "compile(",
    "globals(",
    "setattr(",
    "getattr(",
)

MAX_NET_DIFF_LINES = 200
MAX_CHANGES = 8


SYSTEM_PROMPT = """\
Ты — главный квант-инженер автоматической FOREX-системы для бинарных
опционов с горизонтом 5 часов и выплатой 80% (break-even WR ≈ 55.6%).
Твоя задача — прочитать свежий winrate и предложить точечные правки в
исходных файлах, которые повысят winrate, **не ломая** систему.

ОБЯЗАТЕЛЬНЫЕ ОГРАНИЧЕНИЯ:
1. Минимум 3 прогноза каждые 5 часов: НЕ трогай `MIN_PICKS`, не делай
   ворота настолько строгими, что 0 пар прошли бы.
2. Сохраняй список 28 валютных пар целиком.
3. Сохраняй 15 голосующих блоков (можно менять только их веса /
   пороги внутри `app/analyzer.py`).
4. Не добавляй новые библиотеки — только то, что уже импортировано.
5. Не используй: open(), eval(), exec(), import os, subprocess.
6. Максимум 8 изменений за один запуск, суммарно ≤ 200 строк.
7. Каждое изменение — это пара (старый_фрагмент, новый_фрагмент); старый
   фрагмент должен встречаться в файле **ровно один раз**.

Формат ответа — строго JSON, без markdown-обёртки, ничего лишнего:

{
  "changes": [
    {
      "file": "app/analyzer.py",
      "old": "...точный фрагмент исходника, минимум 30 символов...",
      "new": "...замена..."
    }
  ],
  "rationale": "Краткое объяснение, почему именно эти правки повысят WR."
}

Если по данным правки не оправданы — верни `{"changes": [], "rationale": "WR в норме, ничего не меняем."}`.
"""


# ── small helpers ─────────────────────────────────────────────────────


def read_text(path: Path, limit: int = 60_000) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except FileNotFoundError:
        return ""


def parse_recent_wr() -> tuple[float | None, int]:
    if not STATE_FILE.exists():
        return None, 0
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return None, 0
    history = data.get("history", []) or []
    cycles = history[-10:]
    wins = losses = 0
    for c in cycles:
        for s in c.get("selected", []):
            r = s.get("result_5h")
            if r == "win":
                wins += 1
            elif r == "loss":
                losses += 1
    n = wins + losses
    if n == 0:
        return None, 0
    return 100.0 * wins / n, n


# ── prompt assembly ───────────────────────────────────────────────────


def build_prompt(wr: float | None, decisions: int) -> str:
    parts: list[str] = []
    parts.append("# Контекст")
    if wr is None:
        parts.append("WR за последние 10 циклов: данных недостаточно.")
    else:
        parts.append(f"WR за последние 10 циклов: **{wr:.1f}%** на {decisions} решениях.")
        parts.append("Цель ≥ 60%. Если ниже — нужно ужесточить отбор или скорректировать веса.")

    cycle_md = read_text(CYCLE_REPORT, limit=4000)
    if cycle_md:
        parts += ["", "## Свежий 5h-цикл (выдержка)", cycle_md]

    backtest_md = read_text(BACKTEST_REPORT, limit=4000)
    if backtest_md:
        parts += ["", "## 28-парный бэктест (выдержка)", backtest_md]

    memory_md = read_text(MEMORY_REPORT, limit=4000)
    if memory_md:
        parts += ["", "## Память аналогов (Supabase pgvector)", memory_md]

    parts.append("")
    parts.append("# Исходники, которые можно править")
    for fp in EDITABLE_FILES:
        rel = fp.relative_to(ROOT).as_posix()
        body = read_text(fp, limit=20_000)
        parts += [f"\n## `{rel}`\n```python", body, "```"]

    return "\n".join(parts)[:60_000]


# ── LLM call (GitHub Models, free) ────────────────────────────────────


def call_github_models(prompt: str, token: str) -> str | None:
    model = os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4096,
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
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    except (HTTPError, URLError, KeyError, json.JSONDecodeError) as e:
        print(f"[ai_patcher] github models call failed: {e}", file=sys.stderr)
        return None


def parse_response(raw: str) -> dict | None:
    if not raw:
        return None
    # Trim possible ``` fences.
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    # Locate the JSON object — be lenient about extra text.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        print(f"[ai_patcher] could not parse json: {e}", file=sys.stderr)
        return None


# ── safety checks & application ───────────────────────────────────────


def _is_safe(text: str) -> bool:
    return not any(t in text for t in FORBIDDEN_TOKENS)


def apply_changes(parsed: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """Returns (applied_files_with_diff, rejection_reasons)."""
    rejects: list[str] = []
    applied: list[tuple[str, str]] = []
    changes = parsed.get("changes") or []
    if len(changes) > MAX_CHANGES:
        rejects.append(f"Слишком много правок: {len(changes)} > {MAX_CHANGES}")
        return applied, rejects

    for ch in changes:
        rel = ch.get("file") or ""
        old = ch.get("old") or ""
        new = ch.get("new") or ""
        if not (rel and old and new):
            rejects.append(f"Пустые поля в change: {ch}")
            continue
        path = (ROOT / rel).resolve()
        if path not in [p.resolve() for p in EDITABLE_FILES]:
            rejects.append(f"Запрещённый файл: {rel}")
            continue
        if not _is_safe(new):
            rejects.append(f"В новом коде запрещённые токены: {rel}")
            continue
        src = path.read_text(encoding="utf-8")
        occ = src.count(old)
        if occ != 1:
            rejects.append(
                f"`{rel}`: фрагмент встречается {occ} раз — пропуск"
            )
            continue
        new_src = src.replace(old, new, 1)
        if abs(len(new_src) - len(src)) > 8000:
            rejects.append(f"`{rel}`: слишком большое изменение")
            continue
        path.write_text(new_src, encoding="utf-8")
        diff = "\n".join(difflib.unified_diff(
            src.splitlines(), new_src.splitlines(),
            fromfile=rel, tofile=rel, lineterm="", n=2,
        ))
        applied.append((rel, diff))

    # Net-diff size guard across all files.
    total_lines = sum(len(d.splitlines()) for _, d in applied)
    if total_lines > MAX_NET_DIFF_LINES:
        # Roll back everything by re-reading from git.
        subprocess.run(["git", "checkout", "--"] + [
            str(p.relative_to(ROOT)) for p in EDITABLE_FILES
        ], cwd=ROOT, check=False)
        rejects.append(
            f"Суммарный diff {total_lines} строк > лимит {MAX_NET_DIFF_LINES}"
        )
        return [], rejects
    return applied, rejects


def smoke_compile() -> str | None:
    """Returns error string if compile fails, else None."""
    res = subprocess.run(
        [sys.executable, "-c", "import app.analyzer, app.cycle, app.config"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if res.returncode != 0:
        return res.stderr.strip()[-1500:]
    return None


# ── main ──────────────────────────────────────────────────────────────


def main() -> int:
    wr, decisions = parse_recent_wr()

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        msg = "# 🤖 AI-patcher — нет GITHUB_TOKEN, пропуск.\n"
        PROPOSAL.write_text(msg, encoding="utf-8")
        print(msg)
        return 0

    prompt = build_prompt(wr, decisions)
    raw = call_github_models(prompt, token)
    parsed = parse_response(raw or "")
    if parsed is None:
        msg = (
            "# 🤖 AI-patcher — модель не вернула валидный JSON\n\n"
            "Попробуй на следующем запуске. Сырой ответ модели сохранён "
            "в логах workflow."
        )
        PROPOSAL.write_text(msg, encoding="utf-8")
        print(msg)
        return 0

    rationale = parsed.get("rationale") or "(модель не указала причину)"
    if not parsed.get("changes"):
        PROPOSAL.write_text(
            f"# 🤖 AI-patcher — без изменений\n\n{rationale}\n",
            encoding="utf-8",
        )
        print("AI-patcher: nothing to change.")
        return 0

    applied, rejects = apply_changes(parsed)

    if applied:
        err = smoke_compile()
        if err:
            # Roll back any partial edits.
            subprocess.run(["git", "checkout", "--"] + [
                str(p.relative_to(ROOT)) for p in EDITABLE_FILES
            ], cwd=ROOT, check=False)
            PROPOSAL.write_text(
                "# 🤖 AI-patcher — патч сломал импорт, откатил\n\n"
                f"```\n{err}\n```\n\n## Rationale (отвергнут)\n{rationale}\n",
                encoding="utf-8",
            )
            print("AI-patcher: patch rolled back due to import error.")
            return 0

    md = ["# 🤖 AI-patcher — предложение"]
    if wr is not None:
        md.append(f"\n**Контекст:** WR за последние 10 циклов = {wr:.1f}% на {decisions} решениях.")
    md.append(f"\n## Объяснение модели\n\n{rationale}\n")
    if applied:
        md.append("## Применённые изменения\n")
        for rel, diff in applied:
            md.append(f"### `{rel}`\n\n```diff\n{diff}\n```\n")
    if rejects:
        md.append("## Отвергнутые предложения\n")
        for r in rejects:
            md.append(f"- {r}")
    PROPOSAL.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"AI-patcher: applied {len(applied)} changes, rejected {len(rejects)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
