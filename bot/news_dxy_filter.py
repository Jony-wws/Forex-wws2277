"""
News & DXY filter module for v26.
Free APIs only:
- ForexFactory RSS for news calendar
- Yahoo Finance (yfinance) for DXY
- Pivot points calculated from recent OHLC
"""
from __future__ import annotations
import os, json, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

import feedparser
import pandas as pd
import numpy as np

CACHE_DIR = Path("/home/ubuntu/deriv_bot/cache")
CACHE_DIR.mkdir(exist_ok=True)

FF_RSS = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
NEWS_BLACKOUT_MIN = 30  # ±30 min around high-impact news

PAIR_CURRENCIES = {
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"),
    "AUDUSD": ("AUD", "USD"), "NZDUSD": ("NZD", "USD"),
    "USDJPY": ("USD", "JPY"), "USDCAD": ("USD", "CAD"),
    "EURJPY": ("EUR", "JPY"), "GBPJPY": ("GBP", "JPY"),
    "AUDJPY": ("AUD", "JPY"), "CADJPY": ("CAD", "JPY"),
    "EURGBP": ("EUR", "GBP"), "EURCHF": ("EUR", "CHF"),
    "EURAUD": ("EUR", "AUD"), "GBPCHF": ("GBP", "CHF"),
}

# Map pair to USD-side: True if USD is base, False if quote
USD_RELATION = {
    "EURUSD": ("USD_QUOTE", -1),  # USD up → EURUSD down
    "GBPUSD": ("USD_QUOTE", -1),
    "AUDUSD": ("USD_QUOTE", -1),
    "NZDUSD": ("USD_QUOTE", -1),
    "USDJPY": ("USD_BASE", +1),  # USD up → USDJPY up
    "USDCAD": ("USD_BASE", +1),
}


def parse_ff_rss(cache_max_age_min: int = 60) -> list:
    """Parse ForexFactory RSS for this week's events. Cache to disk."""
    cache = CACHE_DIR / "ff_events.json"
    if cache.exists():
        age_min = (time.time() - cache.stat().st_mtime) / 60
        if age_min < cache_max_age_min:
            try:
                return json.loads(cache.read_text())
            except: pass
    try:
        feed = feedparser.parse(FF_RSS)
        events = []
        for e in feed.entries:
            try:
                # Format: "Mon, 28 Apr 2026 13:30:00 -0400" -- parse without TZ
                ts_str = e.pubDate
                if ts_str.endswith(" GMT"):
                    ts = datetime.strptime(ts_str, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
                else:
                    # Handles "+0000", "-0400", etc.
                    ts = datetime.strptime(ts_str[:-6], "%a, %d %b %Y %H:%M:%S")
                    # Apply offset
                    sign = 1 if ts_str[-5] == "+" else -1
                    off_h = int(ts_str[-4:-2]); off_m = int(ts_str[-2:])
                    ts = ts - sign * timedelta(hours=off_h, minutes=off_m)
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            country = getattr(e, "country", "")
            impact = getattr(e, "impact", "").lower()
            title = getattr(e, "title", "")
            events.append({"ts": ts.isoformat(), "country": country, "impact": impact, "title": title})
        cache.write_text(json.dumps(events))
        return events
    except Exception as ex:
        print(f"FF RSS error: {ex}")
        return []


def is_news_blackout(pair: str, now_utc: datetime, blackout_min: int = NEWS_BLACKOUT_MIN) -> tuple[bool, str]:
    """Return (is_blacked_out, reason)."""
    base, quote = PAIR_CURRENCIES.get(pair, ("", ""))
    relevant_currencies = {base, quote}
    events = parse_ff_rss()
    for e in events:
        if e["impact"] != "high": continue
        if e["country"] not in relevant_currencies: continue
        try:
            ts = datetime.fromisoformat(e["ts"])
        except: continue
        delta_min = abs((ts - now_utc).total_seconds() / 60)
        if delta_min <= blackout_min:
            return True, f"BLACKOUT {e['country']} HIGH at {ts.strftime('%H:%M UTC')}: {e['title']}"
    return False, ""


# === DXY ===
_DXY_CACHE = {"ts": 0, "data": None}

def get_dxy_trend(cache_max_age_min: int = 30) -> dict | None:
    """Returns dict with current, change_24h_pct, trend ('up','down','flat'). None if unavailable."""
    if time.time() - _DXY_CACHE["ts"] < cache_max_age_min * 60 and _DXY_CACHE["data"]:
        return _DXY_CACHE["data"]
    try:
        import yfinance as yf
        df = yf.download("DX-Y.NYB", period="3d", interval="1h", progress=False, auto_adjust=False)
        if len(df) < 2: return None
        closes = df["Close"].values.flatten()
        last = float(closes[-1])
        d24 = float(closes[-24]) if len(closes) >= 24 else float(closes[0])
        ch = (last - d24) / d24 * 100
        if abs(ch) < 0.10:
            trend = "flat"
        elif ch > 0:
            trend = "up"
        else:
            trend = "down"
        result = {"current": last, "change_24h_pct": ch, "trend": trend}
        _DXY_CACHE["ts"] = time.time()
        _DXY_CACHE["data"] = result
        return result
    except Exception as e:
        print(f"DXY error: {e}")
        return None


def dxy_aligned(pair: str, direction: str) -> tuple[bool, str]:
    """Check if trade direction aligns with DXY trend (only matters for USD pairs)."""
    rel = USD_RELATION.get(pair)
    if rel is None:
        return True, "non-USD pair, DXY n/a"
    dxy = get_dxy_trend()
    if dxy is None:
        return True, "DXY unavailable, skipping check"
    if dxy["trend"] == "flat":
        return True, f"DXY flat ({dxy['change_24h_pct']:+.2f}%)"
    pos, sign = rel  # sign=+1 means USD↑ → pair↑
    # If DXY up and pair has USD as base → pair likely up → BUY aligned
    # If DXY up and pair has USD as quote → pair likely down → SELL aligned
    if pos == "USD_BASE":
        # USD is base: BUY aligned with DXY up, SELL aligned with DXY down
        ok = (dxy["trend"] == "up" and direction == "BUY") or (dxy["trend"] == "down" and direction == "SELL")
    else:  # USD_QUOTE
        ok = (dxy["trend"] == "up" and direction == "SELL") or (dxy["trend"] == "down" and direction == "BUY")
    if ok:
        return True, f"DXY {dxy['trend']} ({dxy['change_24h_pct']:+.2f}%) aligned with {direction}"
    else:
        return False, f"DXY {dxy['trend']} ({dxy['change_24h_pct']:+.2f}%) AGAINST {direction}"


# === S/R levels ===
def calc_pivot(df_15m: pd.DataFrame) -> dict | None:
    """Return classic pivot R1/S1/R2/S2 from previous calendar day."""
    if df_15m is None or len(df_15m) < 96 * 2: return None
    daily = df_15m.resample("1D").agg({"High": "max", "Low": "min", "Close": "last"}).dropna()
    if len(daily) < 2: return None
    prev = daily.iloc[-2]  # previous full day
    ph = float(prev["High"]); pl = float(prev["Low"]); pc = float(prev["Close"])
    pivot = (ph + pl + pc) / 3
    r1 = 2 * pivot - pl
    s1 = 2 * pivot - ph
    r2 = pivot + (ph - pl)
    s2 = pivot - (ph - pl)
    return {"pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2,
            "prev_high": ph, "prev_low": pl, "prev_close": pc}


def near_sr(price: float, sr: dict, threshold_pct: float = 0.05) -> tuple[bool, str]:
    """Return (is_near, level_name) if price within threshold_pct of any S/R level."""
    if not sr: return False, ""
    for name in ["pivot", "r1", "s1", "r2", "s2"]:
        level = sr[name]
        diff_pct = abs(price - level) / level * 100
        if diff_pct < threshold_pct:
            return True, f"{name}={level:.5f} ({diff_pct:.2f}% away)"
    return False, ""


if __name__ == "__main__":
    # Self-test
    now = datetime.now(timezone.utc)
    print(f"=== news_dxy_filter test @ UTC {now.strftime('%H:%M')} ===")
    print(f"\n📰 News blackout USDJPY: {is_news_blackout('USDJPY', now)}")
    print(f"📰 News blackout EURUSD: {is_news_blackout('EURUSD', now)}")
    print(f"\n💵 DXY: {get_dxy_trend()}")
    print(f"\nDXY align checks:")
    for p, d in [("USDJPY","SELL"),("USDJPY","BUY"),("NZDUSD","BUY"),("EURUSD","SELL"),("EURJPY","BUY")]:
        ok, msg = dxy_aligned(p, d)
        print(f"  {p:<7} {d}: aligned={ok} — {msg}")
