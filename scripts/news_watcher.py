"""Watch ForexFactory RSS for high-impact news and alert via Telegram
2 hours before red-flagged events.

Runs every hour via GitHub Actions.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

TZ_UTC5 = timezone(timedelta(hours=5))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
SEEN_FILE = os.path.join(STATE_DIR, "news_seen.json")
RSS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

CURRENCY_TO_PAIRS = {
    "USD": ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"],
    "EUR": ["EURUSD", "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD"],
    "GBP": ["GBPUSD", "EURGBP", "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD"],
    "JPY": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY"],
    "AUD": ["AUDUSD", "EURAUD", "GBPAUD", "AUDJPY", "AUDCAD", "AUDCHF", "AUDNZD"],
    "CAD": ["USDCAD", "EURCAD", "GBPCAD", "CADJPY", "AUDCAD", "CADCHF", "NZDCAD"],
    "CHF": ["USDCHF", "EURCHF", "GBPCHF", "CHFJPY", "AUDCHF", "CADCHF", "NZDCHF"],
    "NZD": ["NZDUSD", "EURNZD", "GBPNZD", "NZDJPY", "AUDNZD", "NZDCAD", "NZDCHF"],
}


def fetch_rss() -> list[dict]:
    """Fetch ForexFactory RSS and parse events."""
    try:
        req = urllib.request.Request(
            RSS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Forex-wws2277 news watcher)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode()
    except Exception as e:
        print(f"[news] fetch failed: {e}")
        return []

    try:
        root = ET.fromstring(data)
    except Exception as e:
        print(f"[news] parse failed: {e}")
        return []

    events: list[dict] = []
    for item in root.iter("event"):
        title = (item.findtext("title") or "").strip()
        country = (item.findtext("country") or "").strip().upper()
        date_str = (item.findtext("date") or "").strip()
        time_str = (item.findtext("time") or "").strip()
        impact = (item.findtext("impact") or "").strip().lower()
        if not title or impact != "high":
            continue
        # Parse the datetime — ForexFactory uses MM-DD-YYYY HH:MMam/pm format
        dt = parse_ff_datetime(date_str, time_str)
        if not dt:
            continue
        events.append({
            "title": title,
            "country": country,
            "datetime": dt.isoformat(),
            "impact": impact,
        })
    return events


def parse_ff_datetime(date_str: str, time_str: str) -> datetime | None:
    """Parse ForexFactory date+time strings into UTC datetime.

    `date_str` examples: "11-04-2025", "12-31-2025"  (MM-DD-YYYY)
    `time_str` examples: "8:30am", "12:00pm", "2:00pm", "All Day", ""
    """
    if not date_str or "All Day" in (time_str or "") or not time_str:
        return None
    try:
        # Combine — ForexFactory times are US Eastern (NY).
        combined = f"{date_str} {time_str}"
        dt = datetime.strptime(combined, "%m-%d-%Y %I:%M%p")
        # Treat as US Eastern for simplicity (UTC-5 winter / UTC-4 summer);
        # use UTC-5 fixed for now (close enough for 2h-warning purposes).
        dt = dt.replace(tzinfo=timezone(timedelta(hours=-5)))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def load_seen() -> dict:
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        return json.load(open(SEEN_FILE))
    except Exception:
        return {}


def save_seen(seen: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(seen, open(SEEN_FILE, "w"), indent=2, default=str)


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[news] no Telegram credentials — skip")
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
        print(f"[news] telegram failed: {e}")
        return False


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    events = fetch_rss()
    print(f"[news] fetched {len(events)} high-impact events this week")

    seen = load_seen()
    # Forget events older than 1 week
    cutoff = (now_utc - timedelta(days=7)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}

    alerts_sent = 0
    for evt in events:
        evt_id = f"{evt['country']}_{evt['title']}_{evt['datetime']}"
        if evt_id in seen:
            continue

        try:
            evt_dt = datetime.fromisoformat(evt["datetime"])
        except Exception:
            continue

        seconds_until = (evt_dt - now_utc).total_seconds()
        # Alert when event is between 1.5 and 2.5 hours away
        if 1.5 * 3600 <= seconds_until <= 2.5 * 3600:
            pairs = CURRENCY_TO_PAIRS.get(evt["country"], [])
            pairs_str = ", ".join(pairs[:7]) if pairs else "none mapped"
            evt_local = evt_dt.astimezone(TZ_UTC5)
            text = (
                f"🚨 <b>НОВОСТЬ ВЫСОКОЙ ВАЖНОСТИ ЧЕРЕЗ ~2 ЧАСА</b>\n"
                f"\n<b>{evt['country']}</b>: {evt['title']}\n"
                f"Время: <b>{evt_local.strftime('%H:%M UTC+5')}</b>\n"
                f"Затронутые пары: {pairs_str}\n\n"
                f"<i>Рекомендую закрыть открытые сделки по этим парам "
                f"и не открывать новые ±1 час от события.</i>"
            )
            if send_telegram(text):
                seen[evt_id] = now_utc.isoformat()
                alerts_sent += 1
                print(f"[news] alerted: {evt['country']} {evt['title']} @ {evt_local}")

    save_seen(seen)
    print(f"[news] sent {alerts_sent} alert(s)")


if __name__ == "__main__":
    main()
