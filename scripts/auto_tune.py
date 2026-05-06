"""Auto-tuner — reads recent winrates and proposes threshold edits.

Runs daily on GitHub Actions. Logic:

1. Read the rolling 5h cycle history (`state/cycle_*.json`) and compute
   the average winrate over the last N cycles (default 8 ≈ 40 hours).
2. If WR < 55 % AND we have at least 4 finished cycles, suggest tightening
   the strict gate by one notch (e.g. STRONG_CONFIDENCE 88 → 90,
   STRONG_PERSISTENCE 80 → 100).
3. If WR > 75 % AND the strict gate has been raised in the past, suggest
   relaxing one notch back to a more permissive level so we still get
   ≥3 strong picks per cycle.
4. Either way, write the proposal to ``reports/auto_tune_proposal.md``
   and edit ``app/cycle.py`` in-place. The accompanying workflow then
   commits the patch as a PR for the user to review.

NO paid LLM. Pure rules over the existing JSON state.

Run locally:
    python scripts/auto_tune.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)

CYCLE_FILE = ROOT / "state" / "forecasts.json"
CYCLE_PY = ROOT / "app" / "cycle.py"
PROPOSAL = REPORTS / "auto_tune_proposal.md"

# Notches the auto-tuner is allowed to move thresholds through. The order
# inside each list is from MOST PERMISSIVE (index 0) to STRICTEST.
NOTCHES = {
    "STRONG_CONFIDENCE": [85, 88, 90, 92],
    "STRONG_RATIO": [0.50, 0.55, 0.60, 0.65],
    "STRONG_ADX_H1": [22.0, 25.0, 28.0, 30.0],
    "STRONG_ADX_H4": [18.0, 20.0, 22.0, 25.0],
    "STRONG_PERSISTENCE": [60.0, 80.0, 100.0],
}

# Heuristic targets.
TARGET_WR = 60.0          # break-even for 80 % binary is ~55.6 %
LOW_WR = 55.0
HIGH_WR = 75.0
MIN_CYCLES_FOR_DECISION = 4


# ── current threshold parsing ──────────────────────────────────────────


def read_current(name: str) -> float | int | None:
    src = CYCLE_PY.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\s*=\s*([0-9.]+)", src, re.MULTILINE)
    if not m:
        return None
    raw = m.group(1)
    return float(raw) if "." in raw else int(raw)


def write_threshold(name: str, new_value: float | int) -> bool:
    src = CYCLE_PY.read_text(encoding="utf-8")
    formatted = repr(new_value)
    new_src, n = re.subn(
        rf"^({re.escape(name)}\s*=\s*)[0-9.]+",
        rf"\g<1>{formatted}",
        src,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 1:
        CYCLE_PY.write_text(new_src, encoding="utf-8")
        return True
    return False


# ── winrate calculation ───────────────────────────────────────────────


def recent_wr(window: int = 8) -> tuple[float | None, int, list[str]]:
    if not CYCLE_FILE.exists():
        return None, 0, []
    try:
        data = json.loads(CYCLE_FILE.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return None, 0, []
    history = data.get("history", []) or []
    cycles = [c for c in history if c.get("evaluated_5h_count")]
    cycles = cycles[-window:] if cycles else history[-window:]
    wins = losses = 0
    samples: list[str] = []
    for c in cycles:
        for s in c.get("selected", []):
            r = s.get("result_5h")
            if r == "win":
                wins += 1
            elif r == "loss":
                losses += 1
        samples.append(c.get("cycle_start_utc", "?"))
    decisions = wins + losses
    if decisions == 0:
        return None, 0, samples
    return 100.0 * wins / decisions, decisions, samples


# ── proposal logic ────────────────────────────────────────────────────


def propose_changes(wr: float) -> list[tuple[str, float | int, float | int]]:
    """Return list of (name, old, new) tuples for parameter changes."""
    changes: list[tuple[str, float | int, float | int]] = []
    if wr < LOW_WR:
        # Tighten — move one notch up.
        for name, ladder in NOTCHES.items():
            cur = read_current(name)
            if cur is None:
                continue
            try:
                idx = ladder.index(cur)
            except ValueError:
                continue
            if idx + 1 < len(ladder):
                changes.append((name, cur, ladder[idx + 1]))
    elif wr > HIGH_WR:
        # Relax — move one notch down so we still get ≥ 3 strong picks.
        for name, ladder in NOTCHES.items():
            cur = read_current(name)
            if cur is None:
                continue
            try:
                idx = ladder.index(cur)
            except ValueError:
                continue
            if idx - 1 >= 0:
                changes.append((name, cur, ladder[idx - 1]))
    return changes


# ── main ──────────────────────────────────────────────────────────────


def main() -> int:
    wr, decisions, samples = recent_wr()
    if wr is None:
        msg = (
            "# 🔧 Auto-tuner — нет данных\n\n"
            "Недостаточно завершённых циклов в `state/forecasts.json`. "
            "Подожди ещё несколько 5-часовых окон."
        )
        PROPOSAL.write_text(msg, encoding="utf-8")
        print(msg)
        return 0

    if len(samples) < MIN_CYCLES_FOR_DECISION:
        msg = (
            f"# 🔧 Auto-tuner — рано принимать решение\n\n"
            f"WR за {len(samples)} циклов = {wr:.1f}% ({decisions} решений). "
            f"Нужно ≥ {MIN_CYCLES_FOR_DECISION}, ждём."
        )
        PROPOSAL.write_text(msg, encoding="utf-8")
        print(msg)
        return 0

    changes = propose_changes(wr)
    lines = [
        "# 🔧 Auto-tuner — авто-предложение порогов",
        "",
        f"**Окно:** последние {len(samples)} циклов · {decisions} решений",
        f"**WR:** {wr:.1f}% (цель ≥ {TARGET_WR:.0f}%)",
        "",
    ]
    if not changes:
        lines += [
            "**Действие:** ничего не менять — текущая настройка адекватна.",
            f"WR в коридоре {LOW_WR:.0f}% – {HIGH_WR:.0f}%, "
            "система работает в пределах ожиданий.",
        ]
        PROPOSAL.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        return 0

    direction = "ужесточаем" if wr < LOW_WR else "ослабляем"
    lines.append(f"**Действие:** {direction} строгие ворота на одну ступень.")
    lines.append("")
    lines.append("| Параметр | Было | Станет |")
    lines.append("|---|---:|---:|")
    for name, old, new in changes:
        lines.append(f"| `{name}` | {old} | **{new}** |")

    if os.getenv("AUTO_TUNE_APPLY", "1") == "1":
        applied = []
        for name, old, new in changes:
            if write_threshold(name, new):
                applied.append((name, old, new))
        lines.append("")
        lines.append(f"Изменения применены к `app/cycle.py` ({len(applied)} шт.).")
        lines.append("Workflow откроет PR для вашего ревью.")
    else:
        lines.append("")
        lines.append(
            "_DRY RUN_ — переменная `AUTO_TUNE_APPLY=0`. "
            "Изменения **не** записаны в файл."
        )

    PROPOSAL.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
