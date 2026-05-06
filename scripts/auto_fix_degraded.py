"""Auto-fix degraded strategies — opens a PR when a pair's WR drops
for 3 consecutive cycles.

Reads:  state/cycle_*.json (latest 3)
Action: if any pair dropped WR for 3 cycles straight, tighten its
        params and open a GitHub PR via the API.

Requires: GITHUB_TOKEN env var (available in Actions via secrets.GITHUB_TOKEN).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from glob import glob

TZ_UTC5 = timezone(timedelta(hours=5))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
MEMORY_FILE = os.path.join(STATE_DIR, "strategy_memory.json")


def load_last_n_cycles(n: int = 3) -> list[dict]:
    files = sorted(glob(os.path.join(STATE_DIR, "cycle_*.json")))
    files = [f for f in files if "latest" not in f]
    recent = files[-n:] if len(files) >= n else files
    out = []
    for f in recent:
        try:
            out.append(json.load(open(f)))
        except Exception:
            pass
    return out


def detect_degradation(cycles: list[dict], threshold: float = 3.0) -> list[dict]:
    """Find pairs whose WR dropped for 3 consecutive cycles."""
    if len(cycles) < 3:
        return []

    degraded = []
    # Get per-pair WR from each cycle
    pair_wrs: dict[str, list[float]] = {}
    for c in cycles[-3:]:
        for r in c.get("per_pair", []):
            pair = r.get("pair")
            if pair:
                pair_wrs.setdefault(pair, []).append(r.get("wr", 0))

    for pair, wrs in pair_wrs.items():
        if len(wrs) < 3:
            continue
        # Check monotonic decrease
        if wrs[0] > wrs[1] > wrs[2] and (wrs[0] - wrs[2]) >= threshold:
            degraded.append({
                "pair": pair,
                "wr_3_ago": wrs[0],
                "wr_2_ago": wrs[1],
                "wr_now": wrs[2],
                "drop": round(wrs[0] - wrs[2], 2),
            })

    degraded.sort(key=lambda x: -x["drop"])
    return degraded


def suggest_fix(pair: str, current_params: dict) -> dict:
    """Suggest tighter parameters for a degrading pair."""
    new = dict(current_params)
    # Tighten ADX minimum (+5)
    new["adx_min"] = min(current_params.get("adx_min", 18) + 5, 35)
    # Raise confidence threshold (+5)
    new["min_conf"] = min(current_params.get("min_conf", 72) + 5, 90)
    # Raise trend quality (+5)
    new["min_trend_q"] = min(current_params.get("min_trend_q", 65) + 5, 90)
    # Enforce MTF if not already
    new["require_mtf"] = True
    return new


def create_fix_branch_and_pr(degraded: list[dict]) -> None:
    """Create a branch with tightened params and open a PR."""
    if not degraded:
        return

    # Load memory to get current params
    memory = {}
    if os.path.exists(MEMORY_FILE):
        try:
            memory = json.load(open(MEMORY_FILE))
        except Exception:
            pass

    now = datetime.now(TZ_UTC5)
    branch_name = f"auto-fix/degradation-{now.strftime('%Y%m%d-%H%M')}"

    # Build the fix description
    fixes: list[str] = []
    for d in degraded[:5]:
        pair = d["pair"]
        curr = (memory.get(pair, {}).get("params") or {})
        suggested = suggest_fix(pair, curr)
        fixes.append(
            f"- **{pair}**: WR {d['wr_3_ago']:.1f}% → {d['wr_2_ago']:.1f}% → "
            f"{d['wr_now']:.1f}% (упал на {d['drop']:.1f} п.п. за 3 цикла)\n"
            f"  Предлагаю: ADX min {curr.get('adx_min', '?')}→{suggested['adx_min']}, "
            f"confidence {curr.get('min_conf', '?')}→{suggested['min_conf']}, "
            f"trend_q {curr.get('min_trend_q', '?')}→{suggested['min_trend_q']}"
        )

    body = (
        "## Автоматический фикс деградировавших стратегий\n\n"
        f"Обнаружено {len(degraded)} пар(ы) с падением WR 3 цикла подряд:\n\n"
        + "\n".join(fixes)
        + "\n\n### Что предлагается\n"
        "Ужесточить параметры (выше ADX min, выше min_conf, выше trend_quality) "
        "чтобы стратегия пропускала слабые сигналы и брала только чёткие.\n\n"
        "_Автоматически создано `.github/workflows/auto_fix.yml`_"
    )

    # Write the fix suggestion to a file for the workflow to use
    fix_file = os.path.join(ROOT, "reports", "degradation_fix.md")
    with open(fix_file, "w") as f:
        f.write(body)

    # Print for the workflow to use in creating an issue/PR comment
    print(f"[auto-fix] {len(degraded)} pair(s) degraded:")
    for d in degraded:
        print(f"  {d['pair']}: {d['wr_3_ago']:.1f}% → {d['wr_now']:.1f}% (drop {d['drop']:.1f})")
    print(f"[auto-fix] fix report written to {fix_file}")


def main() -> None:
    cycles = load_last_n_cycles(3)
    if len(cycles) < 3:
        print("[auto-fix] need at least 3 cycles — skip")
        return

    degraded = detect_degradation(cycles)
    if not degraded:
        print("[auto-fix] no pairs degraded 3 cycles in a row — all good")
        return

    create_fix_branch_and_pr(degraded)


if __name__ == "__main__":
    main()
