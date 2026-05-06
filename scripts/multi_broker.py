"""Multi-broker aggregator — compares forex bid/ask across free sources.

Sources:
- Yahoo Finance (default — same as the live site)
- ExchangeRate-API (free tier)
- Frankfurter.app (ECB rates, free)
- Open Exchange Rates (only if APP_OXR_KEY is set)

For each pair we compute the *median* of the available sources and the
spread of the discrete sources to the median.  When ≥1 source diverges
by more than 8 pips from the median, a Telegram alert is fired with the
"odd one out" so the user knows which feed lagged."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yfinance as yf

PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "EURJPY", "GBPJPY", "EURGBP",
]
REPO = Path(__file__).resolve().parent.parent
REPORTS = REPO / "reports"
REPORTS.mkdir(exist_ok=True)

PIP_NONJPY = 10000.0
PIP_JPY    = 100.0


def pip_mult(pair: str) -> float:
    return PIP_JPY if pair.endswith("JPY") else PIP_NONJPY


def fetch_yahoo(pair: str) -> Optional[float]:
    try:
        df = yf.download(f"{pair}=X", period="1d", interval="5m",
                         progress=False, auto_adjust=False)
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        print(f"[multi-broker] yahoo {pair} failed: {e}")
        return None


def fetch_exchangerate_api(pair: str) -> Optional[float]:
    base, quote = pair[:3], pair[3:]
    try:
        with urlopen(f"https://open.er-api.com/v6/latest/{base}",
                     timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("result") == "success":
            return float(data["rates"].get(quote, 0)) or None
    except Exception as e:
        print(f"[multi-broker] er-api {pair} failed: {e}")
    return None


def fetch_frankfurter(pair: str) -> Optional[float]:
    base, quote = pair[:3], pair[3:]
    try:
        with urlopen(f"https://api.frankfurter.app/latest?from={base}&to={quote}",
                     timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return float(data.get("rates", {}).get(quote, 0)) or None
    except Exception as e:
        print(f"[multi-broker] frankfurter {pair} failed: {e}")
    return None


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def main() -> int:
    rows: list[dict] = []
    alerts: list[dict] = []

    for pair in PAIRS:
        sources: dict[str, float] = {}
        for name, fn in (
            ("yahoo", fetch_yahoo),
            ("er-api", fetch_exchangerate_api),
            ("frankfurter", fetch_frankfurter),
        ):
            val = fn(pair)
            if val is not None and val > 0:
                sources[name] = val
        if len(sources) < 2:
            continue

        med = median(list(sources.values()))
        pm = pip_mult(pair)
        diffs_pips = {n: (v - med) * pm for n, v in sources.items()}
        worst = max(diffs_pips, key=lambda k: abs(diffs_pips[k]))
        worst_pips = abs(diffs_pips[worst])
        rows.append({
            "pair": pair,
            "median": med,
            "sources": sources,
            "worst_source": worst,
            "worst_pips": worst_pips,
        })
        if worst_pips >= 8.0:
            alerts.append(rows[-1])

    ts = datetime.now(timezone.utc)
    lines = [
        f"# Multi-broker price aggregator — {ts.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "| Пара | Yahoo | ER-API | Frankfurter | Медиана | Макс. отклонение |",
        "|------|-------|--------|-------------|---------|------------------|",
    ]
    for r in rows:
        s = r["sources"]
        lines.append(
            f"| {r['pair']} "
            f"| {s.get('yahoo','—'):.5f} "
            f"| {s.get('er-api','—'):.5f} "
            f"| {s.get('frankfurter','—'):.5f} "
            f"| {r['median']:.5f} "
            f"| {r['worst_source']} {r['worst_pips']:+.1f} пп |"
        )

    (REPORTS / "multi_broker_latest.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print(f"[multi-broker] wrote {len(rows)} pairs ({len(alerts)} alerts)")

    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if bot and chat and alerts:
        msg = "📊 Multi-broker: расхождение цен >8 пп от медианы\n" + \
              "\n".join(
                  f"  • {a['pair']}: {a['worst_source']} лагает на {a['worst_pips']:.1f} пп"
                  for a in alerts[:5]
              )
        try:
            urlopen(Request(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                data=urlencode({"chat_id": chat, "text": msg}).encode()),
                timeout=10)
        except Exception as e:
            print(f"[multi-broker] telegram failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
