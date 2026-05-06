"""Multi-broker aggregator — sanity-check Yahoo against secondary feeds.

**Yahoo Finance is the trusted source of truth** for the live site
(`app/prices.py`).  This workflow exists only to detect the rare case
when Yahoo itself lags or stale-prints — *not* to flag the slower free
feeds as suspicious every 30 minutes.

Sources:
- Yahoo Finance      (primary, same feed the site uses)
- ExchangeRate-API   (free, ECB-style daily fallback)
- Frankfurter.app    (free, ECB rates, daily)

Algorithm:
1. Pull Yahoo + ER-API + Frankfurter for each pair.
2. Pip distance from Yahoo is computed for each secondary source.
3. **Alert ONLY when BOTH secondary sources disagree with Yahoo by
   ≥ 25 pips simultaneously** — that's the only case Yahoo itself is
   plausibly the laggy one.  Single-source noise is silently logged.

Result: no more false "Yahoo vs ExchangeRate расхождение" alerts —
ER-API/Frankfurter naturally lag by tens of pips because they use
end-of-day ECB fixings, not live tick data.  We only ping you when
*both* of them disagree with Yahoo, which is a real anomaly."""
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
                         progress=False, auto_adjust=False, group_by="column")
        if df is None or df.empty:
            return None
        # yfinance can return either a flat DF or a multi-index DF depending
        # on version.  In both cases the last close is the final value of
        # the Close column squeezed to a scalar.
        close = df["Close"]
        if hasattr(close, "values"):
            arr = close.values.flatten()
        else:
            arr = list(close)
        return float(arr[-1]) if len(arr) else None
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


# Yahoo is treated as the source of truth.  An alert is only fired when
# both alternate sources simultaneously disagree with Yahoo by this many
# pips, which would suggest Yahoo itself is lagging.
YAHOO_SUSPECT_PIPS = 25.0


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
        if "yahoo" not in sources or len(sources) < 2:
            continue

        yahoo_px = sources["yahoo"]
        pm = pip_mult(pair)
        diffs_pips = {
            n: (v - yahoo_px) * pm for n, v in sources.items() if n != "yahoo"
        }
        worst_source = (
            max(diffs_pips, key=lambda k: abs(diffs_pips[k])) if diffs_pips else "—"
        )
        worst_pips = abs(diffs_pips[worst_source]) if diffs_pips else 0.0
        rows.append({
            "pair": pair,
            "yahoo": yahoo_px,
            "sources": sources,
            "diffs_pips": diffs_pips,
            "worst_source": worst_source,
            "worst_pips": worst_pips,
        })
        # Only flag Yahoo as suspect when we have ≥ 2 alternate sources
        # AND ALL of them disagree with Yahoo by ≥ YAHOO_SUSPECT_PIPS —
        # that's the only realistic scenario in which Yahoo itself is the
        # laggy feed.  A single ER-API outlier is not enough.
        secondaries = [abs(p) for p in diffs_pips.values()]
        if (
            len(secondaries) >= 2
            and all(p >= YAHOO_SUSPECT_PIPS for p in secondaries)
        ):
            alerts.append(rows[-1])

    ts = datetime.now(timezone.utc)
    lines = [
        f"# Multi-broker price aggregator — {ts.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "_Yahoo Finance — основной источник, остальные показаны только для "
        "проверки на случай если **сам Yahoo лагает**._  "
        f"Алерт срабатывает только когда **обе** альтернативы расходятся с Yahoo "
        f"на ≥ {int(YAHOO_SUSPECT_PIPS)} пп одновременно.",
        "",
        "| Пара | Yahoo (truth) | ER-API | Frankfurter | Δ ER-API | Δ Frank |",
        "|------|---------------|--------|-------------|---------:|--------:|",
    ]
    for r in rows:
        s = r["sources"]
        d = r["diffs_pips"]
        ya = s.get("yahoo")
        er = s.get("er-api")
        fr = s.get("frankfurter")
        lines.append(
            f"| {r['pair']} "
            f"| **{ya:.5f}** "
            f"| {(f'{er:.5f}' if er is not None else '—')} "
            f"| {(f'{fr:.5f}' if fr is not None else '—')} "
            f"| {d.get('er-api', 0):+.1f} пп "
            f"| {d.get('frankfurter', 0):+.1f} пп |"
        )

    (REPORTS / "multi_broker_latest.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print(f"[multi-broker] wrote {len(rows)} pairs ({len(alerts)} yahoo-suspect alerts)")

    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if bot and chat and alerts:
        msg = (
            "⚠ Yahoo, возможно, лагает (обе альтернативы расходятся "
            f"≥ {int(YAHOO_SUSPECT_PIPS)} пп):\n"
        ) + "\n".join(
            f"  • {a['pair']}: ER-API {a['diffs_pips'].get('er-api',0):+.1f} пп, "
            f"Frank {a['diffs_pips'].get('frankfurter',0):+.1f} пп"
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
