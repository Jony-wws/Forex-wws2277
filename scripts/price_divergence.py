"""Compare Yahoo Finance vs exchangerate-api.com on key pairs.
Alert via Telegram if divergence >5 pips (suggests stale data on one side).

Runs every 15 minutes via GitHub Actions.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

TZ_UTC5 = timezone(timedelta(hours=5))

# Major pairs only — exchangerate-api free tier covers all major currencies.
PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
DIVERGENCE_THRESHOLD_PIPS = 5.0


def fetch_yahoo(pair: str) -> float | None:
    """Use Yahoo Finance v8 quote endpoint (no auth)."""
    ticker = f"{pair}=X"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1m&range=1d"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        meta = result[0].get("meta", {})
        return float(meta.get("regularMarketPrice", 0)) or None
    except Exception as e:
        print(f"[divergence] yahoo {pair} failed: {e}")
        return None


def fetch_exchangerate_api(pair: str) -> float | None:
    """Free tier of exchangerate-api.com — no auth, USD-base only."""
    base = pair[:3]
    quote = pair[3:]
    url = f"https://api.exchangerate-api.com/v4/latest/{base}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        rate = data.get("rates", {}).get(quote)
        return float(rate) if rate else None
    except Exception as e:
        print(f"[divergence] exchangerate-api {pair} failed: {e}")
        return None


def pip_mult(pair: str) -> float:
    return 100.0 if "JPY" in pair else 10000.0


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[divergence] no Telegram credentials — skip")
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
    except Exception as e:
        print(f"[divergence] telegram failed: {e}")
        return False


def main() -> None:
    now = datetime.now(TZ_UTC5)
    print(f"[divergence] checking at {now.strftime('%H:%M UTC+5')}")

    alerts: list[str] = []
    for pair in PAIRS:
        y = fetch_yahoo(pair)
        e = fetch_exchangerate_api(pair)
        if y is None or e is None:
            continue
        diff_pips = abs(y - e) * pip_mult(pair)
        print(f"[divergence] {pair}: yahoo {y:.5f} vs exchangerate {e:.5f} = {diff_pips:.1f} pips")
        if diff_pips > DIVERGENCE_THRESHOLD_PIPS:
            alerts.append(
                f"  • <b>{pair}</b>: Yahoo {y:.5f} vs ExchangeRate {e:.5f} "
                f"= <b>{diff_pips:.1f} пп расхождение</b>"
            )

    if alerts:
        text = (
            f"⚠️ <b>РАСХОЖДЕНИЕ ИСТОЧНИКОВ ЦЕНЫ ({now.strftime('%H:%M UTC+5')}):</b>\n\n"
            + "\n".join(alerts)
            + "\n\n<i>Возможно один источник запаздывает или есть рыночный сдвиг. "
            "Перепроверь TradingView перед сделкой.</i>"
        )
        send_telegram(text)
        print(f"[divergence] sent alert with {len(alerts)} divergence(s)")
    else:
        print("[divergence] all sources agree")


if __name__ == "__main__":
    main()
