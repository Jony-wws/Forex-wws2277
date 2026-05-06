"""Weekly performance comparison — runs every Monday at 06:00 UTC.

Compares this week's average WR per pair vs last week, flags degradation,
and sends a summary to Telegram.

Reads:  state/baseline_7d.json + state/cycle_*.json
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from glob import glob

TZ_UTC5 = timezone(timedelta(hours=5))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
REPORTS_DIR = os.path.join(ROOT, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

WR_TARGET = 70.0


def load_cycles_last_n_days(days: int) -> list[dict]:
    """Load all cycle JSON files from the last N days."""
    files = sorted(glob(os.path.join(STATE_DIR, "cycle_*.json")))
    files = [f for f in files if "latest" not in f]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent: list[dict] = []
    for f in files:
        try:
            ts_str = os.path.basename(f).replace("cycle_", "").replace(".json", "")
            ts = datetime.strptime(ts_str, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                recent.append(json.load(open(f)))
        except Exception:
            continue
    return recent


def compute_weekly_stats(cycles: list[dict]) -> dict[str, dict]:
    """Average WR per pair across multiple cycles."""
    pair_wrs: dict[str, list[float]] = {}
    for c in cycles:
        for r in c.get("per_pair", []):
            pair = r.get("pair")
            wr = r.get("wr", 0)
            if pair:
                pair_wrs.setdefault(pair, []).append(wr)
    return {
        pair: {
            "avg_wr": round(sum(wrs) / len(wrs), 2),
            "min_wr": round(min(wrs), 2),
            "max_wr": round(max(wrs), 2),
            "cycles": len(wrs),
        }
        for pair, wrs in pair_wrs.items()
    }


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[weekly] no Telegram credentials — skip")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = []
    while text:
        chunks.append(text[:3900])
        text = text[3900:]
    for chunk in chunks:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=15) as resp:
                resp.read()
        except Exception as e:
            print(f"[weekly] telegram failed: {e}")
            return False
    return True


def main() -> None:
    now = datetime.now(TZ_UTC5)
    print(f"[weekly] generating report at {now.strftime('%Y-%m-%d %H:%M UTC+5')}")

    this_week = load_cycles_last_n_days(7)
    last_week = load_cycles_last_n_days(14)
    # last_week is actually all 14 days — subtract this_week to get "previous 7"
    last_week_only = last_week[:max(0, len(last_week) - len(this_week))]

    stats_now = compute_weekly_stats(this_week)
    stats_prev = compute_weekly_stats(last_week_only)

    out: list[str] = []
    out.append(f"<b>📊 Еженедельный отчёт — {now.strftime('%d.%m.%Y')}</b>")
    out.append(f"<i>Сравнение этой недели ({len(this_week)} циклов) vs прошлой ({len(last_week_only)} циклов)</i>\n")

    # Improvements
    improved: list[tuple[str, float, float]] = []
    degraded: list[tuple[str, float, float]] = []
    for pair, s in stats_now.items():
        prev = stats_prev.get(pair, {})
        if not prev:
            continue
        delta = s["avg_wr"] - prev.get("avg_wr", 0)
        if delta >= 3.0:
            improved.append((pair, prev["avg_wr"], s["avg_wr"]))
        elif delta <= -3.0:
            degraded.append((pair, prev["avg_wr"], s["avg_wr"]))

    if improved:
        improved.sort(key=lambda x: -(x[2] - x[1]))
        out.append("<b>📈 Улучшились (≥3 п.п.):</b>")
        for pair, old, new in improved[:10]:
            out.append(f"  • {pair}: {old:.1f}% → <b>{new:.1f}%</b> ({new - old:+.1f})")
        out.append("")

    if degraded:
        degraded.sort(key=lambda x: x[2] - x[1])
        out.append("<b>📉 Деградировали (≥3 п.п.):</b>")
        for pair, old, new in degraded[:10]:
            out.append(f"  • {pair}: {old:.1f}% → <b>{new:.1f}%</b> ({new - old:+.1f})")
        out.append("")

    # Top-5 best this week
    sorted_pairs = sorted(stats_now.items(), key=lambda x: -x[1]["avg_wr"])
    out.append("<b>🏆 Топ-5 этой недели:</b>")
    for pair, s in sorted_pairs[:5]:
        above = "✅" if s["avg_wr"] >= WR_TARGET else "⚠️"
        out.append(f"  {above} {pair}: avg WR <b>{s['avg_wr']:.1f}%</b> (min {s['min_wr']:.1f}%, max {s['max_wr']:.1f}%)")

    out.append("\n<i>Авто-генерация каждый понедельник.</i>")

    report = "\n".join(out)
    # Save markdown
    md = report.replace("<b>", "**").replace("</b>", "**").replace("<i>", "*").replace("</i>", "*")
    with open(os.path.join(REPORTS_DIR, "weekly_latest.md"), "w") as f:
        f.write(md)
    print(f"[weekly] report saved, {len(improved)} improved, {len(degraded)} degraded")

    send_telegram(report)


if __name__ == "__main__":
    main()
