"""ML-style drift detector for cycle WR series.

Reads ``state/cycle_history.json`` (created automatically by cycle_5h.py)
and applies a simple linear regression on the last N cycles' winrate to
predict whether the strategy is degrading.  When the predicted WR for
the *next* cycle is ≥ 5 percentage points below the current best, opens
an alert (Telegram + a markdown file).

This is intentionally lightweight — no scikit-learn dep, just numpy.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

REPO = Path(__file__).resolve().parent.parent
HIST = REPO / "state" / "cycle_history.json"
REPORTS = REPO / "reports"
REPORTS.mkdir(exist_ok=True)


def load_history() -> list[dict[str, Any]]:
    if not HIST.exists():
        return []
    try:
        return json.loads(HIST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def linear_predict(ys: list[float], n_ahead: int = 1) -> float:
    """Best-fit linear extrapolation of the next value."""
    if len(ys) < 3:
        return float(ys[-1]) if ys else 0.0
    xs = np.arange(len(ys), dtype=float)
    a, b = np.polyfit(xs, ys, 1)
    return float(a * (len(ys) + n_ahead - 1) + b)


def main() -> int:
    history = load_history()
    if len(history) < 5:
        print(f"[drift] need ≥5 cycles, have {len(history)} — nothing to predict")
        return 0

    # Track per-pair WR series.
    per_pair: dict[str, list[float]] = {}
    for cycle in history[-30:]:                       # last 30 cycles only
        for entry in cycle.get("top", []):
            pair = entry.get("pair")
            wr = entry.get("wr")
            if pair and isinstance(wr, (int, float)):
                per_pair.setdefault(pair, []).append(float(wr))

    alerts = []
    for pair, ys in per_pair.items():
        if len(ys) < 5:
            continue
        cur = ys[-1]
        pred = linear_predict(ys, n_ahead=2)
        delta = pred - cur
        if delta <= -5.0 and cur >= 60.0:               # losing ≥5 pp soon
            alerts.append({"pair": pair, "current_wr": cur,
                           "predicted_wr_next2": pred, "delta": delta,
                           "history_len": len(ys)})

    ts = datetime.now(timezone.utc)
    if not alerts:
        print("[drift] no degradation predicted")
        out = REPORTS / "drift_latest.md"
        out.write_text(
            f"# Drift detector — {ts.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            "Деградации в ближайшие 2 цикла не обнаружено.\n",
            encoding="utf-8")
        return 0

    lines = [f"# Drift detector — {ts.strftime('%Y-%m-%d %H:%M UTC')}", ""]
    lines.append("⚠️ Прогнозируется деградация в ближайшие 2 цикла:")
    for a in sorted(alerts, key=lambda x: x["delta"]):
        lines.append(
            f"  • **{a['pair']}** — текущий WR {a['current_wr']:.1f}% → "
            f"прогноз через 2 цикла {a['predicted_wr_next2']:.1f}% "
            f"(Δ {a['delta']:+.1f} пп, история {a['history_len']} циклов)"
        )

    (REPORTS / "drift_latest.md").write_text("\n".join(lines) + "\n",
                                              encoding="utf-8")
    print(f"[drift] wrote drift_latest.md ({len(alerts)} alerts)")

    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if bot and chat and alerts:
        msg = "📉 ML-Drift detector: предсказана деградация\n" + \
              "\n".join(
                  f"  • {a['pair']}: {a['current_wr']:.0f}% → "
                  f"{a['predicted_wr_next2']:.0f}% (Δ {a['delta']:+.0f}пп)"
                  for a in alerts[:3]
              )
        try:
            urlopen(Request(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                data=urlencode({"chat_id": chat, "text": msg}).encode()),
                timeout=10)
        except Exception as e:
            print(f"[drift] telegram failed: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
