"""Reddit r/Forex + r/Daytrading sentiment scraper.

Hourly workflow that pulls recent posts/comments from forex-focused
subreddits, extracts ticker mentions (EURUSD, GBPUSD, etc.), classifies
each as bullish/bearish/neutral via simple keyword sentiment, and writes
a leaderboard to ``reports/sentiment_latest.md``.  Optionally pings
Telegram with the top-3 most-discussed pairs.

No Reddit auth needed — uses the public ``.json`` endpoint with a
descriptive User-Agent.  If Reddit blocks the request, the script logs a
warning and exits cleanly (does not break CI)."""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "EURJPY", "GBPJPY", "EURGBP", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD", "AUDJPY", "AUDCHF", "AUDCAD",
    "AUDNZD", "CADJPY", "CADCHF", "CHFJPY", "NZDJPY", "NZDCHF", "NZDCAD",
]
SUBREDDITS = ["Forex", "Daytrading", "Trading"]
USER_AGENT = "FOREX-cycle-bot/1.0 (sentiment scraper for personal forex tools)"

BULL_WORDS = re.compile(
    r"\b(buy|long|bull|bullish|moon|pump|rip|rally|breakout|uptrend|"
    r"calls?|target up|going up|resistance broken)\b", re.IGNORECASE,
)
BEAR_WORDS = re.compile(
    r"\b(sell|short|bear|bearish|dump|crash|drop|tank|breakdown|"
    r"downtrend|puts?|target down|going down|support broken)\b",
    re.IGNORECASE,
)


def fetch_subreddit(name: str, limit: int = 100) -> list[dict[str, Any]]:
    """Pull the latest ``limit`` posts as JSON from /r/<name>/new.json."""
    url = f"https://www.reddit.com/r/{name}/new.json?limit={limit}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"[sentiment] failed to fetch r/{name}: {e}")
        return []
    children = data.get("data", {}).get("children", [])
    posts = []
    for c in children:
        d = c.get("data", {})
        posts.append({
            "title": d.get("title", ""),
            "body":  d.get("selftext", ""),
            "score": int(d.get("score", 0)),
            "comments": int(d.get("num_comments", 0)),
            "created_utc": int(d.get("created_utc", 0)),
            "url": "https://reddit.com" + d.get("permalink", ""),
        })
    return posts


def classify_post(text: str) -> int:
    """Return +1 bullish / −1 bearish / 0 neutral based on keyword count."""
    bull = len(BULL_WORDS.findall(text))
    bear = len(BEAR_WORDS.findall(text))
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


def extract_pairs(text: str) -> list[str]:
    upper = text.upper().replace("/", "").replace("-", "")
    return [p for p in PAIRS if p in upper]


def main() -> int:
    pair_counts: Counter[str] = Counter()
    pair_sentiment: defaultdict[str, list[int]] = defaultdict(list)
    total_posts = 0

    for sub in SUBREDDITS:
        posts = fetch_subreddit(sub, limit=100)
        total_posts += len(posts)
        for p in posts:
            text = p["title"] + " " + p["body"]
            pairs = extract_pairs(text)
            if not pairs:
                continue
            sent = classify_post(text)
            for pair in pairs:
                pair_counts[pair] += 1
                pair_sentiment[pair].append(sent)

    if total_posts == 0:
        print("[sentiment] all subreddits returned 0 posts — skipping report")
        return 0

    rows: list[tuple[str, int, float, int]] = []
    for pair, n in pair_counts.most_common(15):
        sentiments = pair_sentiment[pair]
        avg_sent = sum(sentiments) / len(sentiments) if sentiments else 0.0
        net_bull = sum(1 for s in sentiments if s > 0)
        rows.append((pair, n, avg_sent, net_bull))

    ts = datetime.now(timezone.utc)
    lines = [
        f"# Reddit Forex Sentiment — {ts.strftime('%Y-%m-%d %H:%M UTC')}",
        f"",
        f"Сканировал {total_posts} свежих постов из r/" + ", r/".join(SUBREDDITS) + ".",
        "",
        "| Пара | Упоминаний | Средн. настроение | Бычьих |",
        "|------|------------|-------------------|--------|",
    ]
    for pair, n, avg, bulls in rows:
        emoji = "🟢" if avg > 0.2 else ("🔴" if avg < -0.2 else "⚪")
        lines.append(f"| {pair} | {n} | {emoji} {avg:+.2f} | {bulls} |")

    out = REPORTS_DIR / "sentiment_latest.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[sentiment] wrote {out}  ({len(rows)} pairs)")

    # Optional Telegram alert when ≥1 pair has strong (≥10 mentions) bias.
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if bot and chat:
        strong = [(p, n, a) for p, n, a, _ in rows if n >= 10 and abs(a) >= 0.4]
        if strong:
            msg = "📣 Reddit sentiment — топ обсуждаемые пары:\n"
            for p, n, a in strong[:3]:
                d = "BULL" if a > 0 else "BEAR"
                msg += f"  • {p}  ({n} упом.)  {d} ({a:+.2f})\n"
            try:
                import urllib.parse
                payload = urllib.parse.urlencode(
                    {"chat_id": chat, "text": msg}).encode()
                urlopen(Request(
                    f"https://api.telegram.org/bot{bot}/sendMessage",
                    data=payload), timeout=10)
            except Exception as e:
                print(f"[sentiment] telegram failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
