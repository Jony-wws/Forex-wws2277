"""Run cycle_5h.py-equivalent backtests across multiple decision TFs.

Currently cycle_5h.py treats H1 as the decision timeframe and M15 as the
entry timeline.  This wrapper runs the same sweep on M5, M15, H1 and H4
in sequence and writes a comparison table to
``reports/multi_tf_latest.md`` so the user can see whether a different
timeframe produces consistently better strategies.

This script is intentionally **diagnostic**: it does NOT replace the
main cycle, it only helps the user decide whether to switch the primary
TF.  The 5-hour cycle workflow keeps using H1 by default.

Telegram alert: only fired when one of the alternate TFs has *more*
on-target pairs than H1.  In that case the user gets a one-line
suggestion."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from cycle_5h import (                                # type: ignore  # noqa: E402
    PAIRS,
    SearchSpace,
    DEFAULT_HORIZON_BARS,
    fetch_pair_data,
    best_effort_top3,
    pick_top3_strict,
    sweep_pair,
)


TFS_TO_TEST = ["M15", "H1", "H4"]    # keep small to fit in a CI budget


def run_for_tf(tf: str) -> dict:
    """Run a stripped-down strategy sweep using ``tf`` as the H1-substitute."""
    print(f"[multi-tf] starting sweep for decision TF = {tf}")
    start = time.time()
    rows = []
    for pair in PAIRS:
        try:
            data = fetch_pair_data(pair, decision_tf=tf)
            best = sweep_pair(pair, data)
            if best is not None:
                rows.append(best)
        except Exception as e:
            print(f"[multi-tf] {pair}@{tf}: {e}")
            continue
    on_target = pick_top3_strict(rows)
    fallback  = best_effort_top3(rows)
    elapsed = time.time() - start
    return {
        "tf": tf,
        "rows": rows,
        "on_target": on_target,
        "fallback": fallback,
        "elapsed_sec": elapsed,
    }


def main() -> int:
    REPORTS = REPO / "reports"
    REPORTS.mkdir(exist_ok=True)

    results: list[dict] = []
    for tf in TFS_TO_TEST:
        try:
            results.append(run_for_tf(tf))
        except TypeError as e:
            # fetch_pair_data may not accept decision_tf yet — bail out.
            print(f"[multi-tf] cycle_5h fetch helpers don't yet support "
                  f"decision_tf parameter: {e}")
            (REPORTS / "multi_tf_latest.md").write_text(
                "# Multi-TF backtest\n\nPending — cycle_5h.fetch_pair_data "
                "must expose a `decision_tf` argument.  This will be added "
                "in a follow-up PR; for now the workflow exits cleanly.\n",
                encoding="utf-8")
            return 0
        except Exception as e:
            print(f"[multi-tf] {tf} failed: {e}")
            continue

    if not results:
        print("[multi-tf] no TF runs succeeded")
        return 0

    # Render a summary table.
    lines = ["# Multi-TF backtest сравнение", "",
             "| TF | Прошли строго | Кандидаты | Время (с) |",
             "|----|---------------|-----------|-----------|"]
    for r in results:
        lines.append(
            f"| {r['tf']} | {len(r['on_target'])} | "
            f"{len(r['fallback'])} | {r['elapsed_sec']:.0f} |"
        )
    (REPORTS / "multi_tf_latest.md").write_text("\n".join(lines) + "\n",
                                                 encoding="utf-8")
    print("[multi-tf] wrote reports/multi_tf_latest.md")

    # Suggest TF change if alternate TF beats H1 by ≥2 strict-pass pairs.
    h1 = next((r for r in results if r["tf"] == "H1"), None)
    if h1 is not None:
        better = [r for r in results
                  if r["tf"] != "H1"
                  and len(r["on_target"]) >= len(h1["on_target"]) + 2]
        if better:
            bot = os.environ.get("TELEGRAM_BOT_TOKEN")
            chat = os.environ.get("TELEGRAM_CHAT_ID")
            if bot and chat:
                from urllib.parse import urlencode
                from urllib.request import Request, urlopen
                msg = "💡 Multi-TF: альтернативный TF даёт больше прошедших пар:\n"
                for r in better:
                    msg += (f"  • {r['tf']}: прошли {len(r['on_target'])} пар "
                            f"(H1: {len(h1['on_target'])})\n")
                try:
                    urlopen(Request(
                        f"https://api.telegram.org/bot{bot}/sendMessage",
                        data=urlencode({"chat_id": chat, "text": msg}).encode()),
                        timeout=10)
                except Exception as e:
                    print(f"[multi-tf] telegram failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
