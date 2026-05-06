"""Generate weekly release notes in Russian from git log.

Walks the last 7 days of commits, groups them by Conventional-Commit
prefix (feat/fix/chore/docs/perf), translates titles to a digestible
Russian summary, and writes ``reports/release_notes_latest.md``.

Optionally pings Telegram with a short summary."""
from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
REPORTS = REPO / "reports"
REPORTS.mkdir(exist_ok=True)


CATEGORY_RU = {
    "feat":   "✨ Новые возможности",
    "fix":    "🐛 Исправления",
    "perf":   "⚡ Производительность",
    "refactor": "♻️ Рефакторинг",
    "docs":   "📚 Документация",
    "chore":  "🔧 Поддержка",
    "test":   "🧪 Тесты",
    "ci":     "🤖 CI / GitHub Actions",
    "build":  "📦 Сборка",
    "style":  "💄 Стиль",
    "other":  "📝 Прочее",
}


def categorise(subject: str) -> str:
    s = subject.lower().strip()
    for prefix in CATEGORY_RU:
        if s.startswith(prefix + ":") or s.startswith(prefix + "(") or s.startswith(prefix + " "):
            return prefix
    return "other"


def main() -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        out = subprocess.run(
            ["git", "log", f"--since={since}", "--pretty=format:%h\t%s",
             "--no-merges"],
            cwd=str(REPO), capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        print(f"[release] git log failed: {e}")
        return 1

    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    total = 0
    for line in out.strip().splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        cat = categorise(subject)
        grouped[cat].append((sha, subject))
        total += 1

    if total == 0:
        print("[release] no commits in last 7 days")
        return 0

    ts = datetime.now(timezone.utc)
    lines = [
        f"# Release notes — неделя до {ts.strftime('%Y-%m-%d')}",
        "",
        f"Всего коммитов: **{total}**",
        "",
    ]
    for cat in CATEGORY_RU:
        items = grouped.get(cat) or []
        if not items:
            continue
        lines.append(f"## {CATEGORY_RU[cat]}")
        lines.append("")
        for sha, subj in items:
            # strip the prefix for cleaner reading
            cleaned = subj.split(":", 1)[-1].strip() if ":" in subj else subj
            lines.append(f"- `{sha}` — {cleaned}")
        lines.append("")

    out_path = REPORTS / "release_notes_latest.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[release] wrote {out_path} ({total} commits)")

    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if bot and chat:
        msg = f"📦 Release notes — {total} коммитов за неделю\n"
        for cat, items in grouped.items():
            if items:
                msg += f"  • {CATEGORY_RU[cat]}: {len(items)}\n"
        try:
            urlopen(Request(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                data=urlencode({"chat_id": chat, "text": msg}).encode()),
                timeout=10)
        except Exception as e:
            print(f"[release] telegram failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
