"""News + geopolitical risk layer for the AI brain.

Pulls two free public RSS feeds:

- ForexFactory weekly calendar (high-impact econ events per currency)
- Reuters Top News + BBC World (geopolitical pulse, headline tagging)

Both are public, free, and require zero auth.  The module produces
two per-currency dictionaries:

    upcoming_event_minutes[curr]  → minutes to next HIGH-impact release
    political_risk[curr]          → integer 0..3 (0 = quiet, 3 = elevated)

The brain uses ``upcoming_event_minutes`` for a hard veto: if a major
release is within 120 minutes for either side of a pair, that pair is
excluded from Top-1 selection — exactly what a disciplined real-money
trader would do.

The module is *robust to network failure*: on any error it returns
empty dicts, which the brain interprets as "no news veto", logged so
ops can spot persistent outages.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import urllib.request

log = logging.getLogger("news_brain")

# Same currencies the macro layer tracks.
CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]


FF_RSS = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
REUTERS_RSS = "https://feeds.reuters.com/reuters/worldNews"
BBC_RSS = "https://feeds.bbci.co.uk/news/world/rss.xml"

# Country / area → currency mapping for headline tagging.
COUNTRY_CURRENCY: dict[str, str] = {
    "united states": "USD",
    "u.s.": "USD",
    "usa": "USD",
    "washington": "USD",
    "federal reserve": "USD",
    "fed": "USD",
    "euro zone": "EUR",
    "eurozone": "EUR",
    "european union": "EUR",
    "ecb": "EUR",
    "germany": "EUR",
    "france": "EUR",
    "italy": "EUR",
    "spain": "EUR",
    "britain": "GBP",
    "uk": "GBP",
    "boe": "GBP",
    "london": "GBP",
    "japan": "JPY",
    "boj": "JPY",
    "tokyo": "JPY",
    "switzerland": "CHF",
    "snb": "CHF",
    "swiss": "CHF",
    "australia": "AUD",
    "rba": "AUD",
    "canada": "CAD",
    "boc": "CAD",
    "new zealand": "NZD",
    "rbnz": "NZD",
}


def _fetch_xml(url: str, timeout: int = 12) -> ET.Element | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Forex-wws2277-news/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ET.fromstring(resp.read())
    except Exception as e:
        log.warning(f"news fetch failed {url}: {e}")
        return None


def fetch_forexfactory_events() -> list[dict]:
    """Return a list of {currency, impact, title, date_utc} events."""
    root = _fetch_xml(FF_RSS)
    if root is None:
        return []
    out: list[dict] = []
    # ForexFactory's "ff_calendar_thisweek.xml" is a flat <weeklyevents>
    # of <event> children.
    for ev in root.iter("event"):
        title = (ev.findtext("title") or "").strip()
        impact = (ev.findtext("impact") or "").strip().lower()
        currency = (ev.findtext("country") or "").strip().upper()
        date_raw = (ev.findtext("date") or "").strip()
        time_raw = (ev.findtext("time") or "").strip()
        if not currency or currency not in CURRENCIES:
            continue
        # Format e.g. "11-15-2026 02:30pm"
        try:
            ts = datetime.strptime(f"{date_raw} {time_raw}", "%m-%d-%Y %I:%M%p")
            ts = ts.replace(tzinfo=timezone.utc)  # ForexFactory feed is UTC.
        except Exception:
            continue
        out.append({
            "title": title,
            "currency": currency,
            "impact": impact,
            "ts_utc": ts.isoformat(),
            "minutes_to": int((ts - datetime.now(timezone.utc)).total_seconds() // 60),
        })
    return out


def next_high_impact_events() -> dict[str, int]:
    """Minutes-to-next-HIGH-impact release per currency.  Missing = 9999."""
    events = fetch_forexfactory_events()
    out = {c: 9999 for c in CURRENCIES}
    for ev in events:
        if ev["impact"] != "high":
            continue
        if ev["minutes_to"] < 0:
            continue
        if ev["minutes_to"] < out[ev["currency"]]:
            out[ev["currency"]] = ev["minutes_to"]
    return out


def fetch_world_headlines(limit: int = 25) -> list[dict]:
    """Combine Reuters + BBC headlines, dedupe, tag by currency keyword."""
    out: list[dict] = []
    for url, source in [(REUTERS_RSS, "Reuters"), (BBC_RSS, "BBC")]:
        root = _fetch_xml(url)
        if root is None:
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if not title:
                continue
            tags = _tag_currencies(title)
            out.append({
                "title": title,
                "link": link,
                "source": source,
                "pub": pub,
                "currencies": tags,
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return out


def _tag_currencies(headline: str) -> list[str]:
    text = headline.lower()
    tags: set[str] = set()
    for keyword, curr in COUNTRY_CURRENCY.items():
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            tags.add(curr)
    return sorted(tags)


def political_risk_scores() -> dict[str, int]:
    """Rough 0..3 geopolitical heat per currency from world headlines.

    The scoring is intentionally simple — high frequency of a country
    in fresh top-news headlines plus risk keywords (war/sanction/
    election/strike) bumps the score.  This is a *risk* signal, not a
    direction signal — the brain uses it to penalise impacted pairs.
    """
    headlines = fetch_world_headlines(limit=40)
    risk_words = re.compile(
        r"\b(war|sanction|sanctions|attack|strike|election|protest|crisis|"
        r"recession|inflation|crash|hack|cyber|coup|missile|tariff)\b"
    )
    risk = {c: 0 for c in CURRENCIES}
    for h in headlines:
        if not risk_words.search(h["title"].lower()):
            continue
        for c in h["currencies"]:
            risk[c] = min(3, risk[c] + 1)
    return risk
