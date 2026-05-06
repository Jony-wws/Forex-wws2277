"""Currency strength heatmap + convergence alerts.

For each of 8 majors (USD, EUR, GBP, JPY, AUD, CAD, NZD, CHF), compute
average % move over the last 5h across all pairs containing that currency.
Adjust sign based on whether the currency is base or quote.

Also detects "convergence events": pairs where 6+ indicators agree on the
same direction simultaneously — historically rare and high-WR.

Runs as part of cycle_5h workflow.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

TZ_UTC5 = timezone(timedelta(hours=5))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
REPORTS_DIR = os.path.join(ROOT, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"]


def compute_strength(per_pair: list[dict]) -> dict[str, float]:
    """For each currency, average the directional move across all pairs.
    Positive = currency strengthening, negative = weakening."""
    moves: dict[str, list[float]] = {c: [] for c in CURRENCIES}
    for r in per_pair:
        pair = r.get("pair", "")
        if len(pair) < 6:
            continue
        base, quote = pair[:3], pair[3:]
        # last_5h_pp is the 5h move in pips for the pair (base/quote)
        # If base/quote moved up, base strengthened, quote weakened.
        move_pp = r.get("last_5h_pp", 0)
        # Normalize by typical daily range (~50 pips for non-JPY)
        pip_norm = 100.0 if "JPY" in pair else 10000.0
        # Convert pips back to % move
        last_close = r.get("last_close", 1.0)
        if last_close == 0:
            continue
        pct = (move_pp / pip_norm) / last_close * 100.0  # % change
        if base in moves:
            moves[base].append(pct)
        if quote in moves:
            moves[quote].append(-pct)
    strength = {}
    for c, vals in moves.items():
        if vals:
            strength[c] = round(sum(vals) / len(vals) * 100, 2)  # bp
        else:
            strength[c] = 0.0
    return strength


def detect_convergence(per_pair: list[dict]) -> list[dict]:
    """Find pairs where confidence ≥ 90% AND ADX ≥ 35 AND all 4 timeframes
    agree — these are 'perfect storm' setups."""
    out = []
    for r in per_pair:
        ind = r.get("indicators_now", {})
        conf = ind.get("confidence", 0)
        adx = ind.get("adx", 0)
        bull_count = ind.get("bull_count", 0)
        bear_count = ind.get("bear_count", 0)
        side = ind.get("side", "")
        all_aligned = (side == "BUY" and bull_count == 4) or (side == "SELL" and bear_count == 4)
        if conf >= 90 and adx >= 35 and all_aligned:
            out.append({
                "pair": r["pair"],
                "side": side,
                "confidence": conf,
                "adx": adx,
                "wr": r.get("wr", 0),
                "trades_per_day": r.get("trades_per_day", 0),
            })
    out.sort(key=lambda x: -x["confidence"])
    return out


def render_heatmap(strength: dict[str, float]) -> str:
    """ASCII heatmap of currency strength."""
    sorted_cs = sorted(strength.items(), key=lambda x: -x[1])
    out = ["<b>💪 Сила валют за 5 часов (ранг):</b>"]
    for c, v in sorted_cs:
        bar_len = min(int(abs(v) / 2), 15)
        bar = ("🟢" * bar_len) if v > 0 else ("🔴" * bar_len)
        out.append(f"  <b>{c}</b>: {v:+.2f} bp  {bar}")
    return "\n".join(out)


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            resp.read()
        return True
    except Exception:
        return False


def main() -> None:
    cycle_path = os.path.join(STATE_DIR, "cycle_latest.json")
    if not os.path.exists(cycle_path):
        print("[strength] no cycle_latest.json — skip")
        return

    cycle = json.load(open(cycle_path))
    per_pair = cycle.get("per_pair", [])
    if not per_pair:
        print("[strength] empty per_pair — skip")
        return

    strength = compute_strength(per_pair)
    convergence = detect_convergence(per_pair)

    now = datetime.now(TZ_UTC5).strftime("%H:%M UTC+5")
    parts = [
        f"<b>📊 Heatmap силы валют — {now}</b>",
        "",
        render_heatmap(strength),
    ]

    if convergence:
        parts.append("\n<b>🔥 КОНВЕРГЕНЦИЯ (6+ индикаторов согласны):</b>")
        for c in convergence[:5]:
            parts.append(
                f"  ⚡ <b>{c['pair']}</b> {c['side']} · "
                f"уверенность {c['confidence']:.0f}% · "
                f"ADX {c['adx']:.0f} · WR {c['wr']:.1f}% · "
                f"{c['trades_per_day']:.1f} сделок/день"
            )
        parts.append("\n<i>Это редкое событие — стоит обратить внимание.</i>")

    text = "\n".join(parts)
    md = text.replace("<b>", "**").replace("</b>", "**").replace("<i>", "*").replace("</i>", "*")

    with open(os.path.join(REPORTS_DIR, "currency_strength_latest.md"), "w") as f:
        f.write(md)
    print(f"[strength] saved heatmap, {len(convergence)} convergence event(s)")

    if convergence or any(abs(v) > 5 for v in strength.values()):
        send_telegram(text)


if __name__ == "__main__":
    main()
