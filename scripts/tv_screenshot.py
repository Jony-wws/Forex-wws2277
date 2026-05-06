"""Take TradingView screenshot for the cycle's #1 pair and commit it.

Reads ``reports/cycle_5h_latest.md`` to find the top-1 pair name, opens
the TradingView chart for that symbol, and saves a PNG into
``screenshots/tv/<PAIR>.png``.  The cycle workflow can then post the
image as a PR comment.

Reuses the storage state created by tv_login.py if available."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "state" / "tv_storage.json"
OUT_DIR = REPO / "screenshots" / "tv"
OUT_DIR.mkdir(parents=True, exist_ok=True)

ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-A546B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
    "Mobile Safari/537.36"
)


def find_top_pair() -> str | None:
    rep = REPO / "reports" / "cycle_5h_latest.md"
    if not rep.exists():
        return None
    text = rep.read_text(encoding="utf-8")
    m = re.search(r"^\s*1\.\s*\*\*?([A-Z]{6})\*?\*?", text, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r"^\s*1\.\s*([A-Z]{6})\b", text, re.MULTILINE)
    return m.group(1) if m else None


def main() -> int:
    pair = os.environ.get("TV_PAIR") or find_top_pair() or "EURUSD"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[tv_screenshot] playwright missing — skipping")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        kwargs = dict(
            user_agent=ANDROID_UA,
            viewport={"width": 412, "height": 915},
            is_mobile=True,
            device_scale_factor=2.625,
            locale="ru-RU",
        )
        if STATE.exists():
            kwargs["storage_state"] = str(STATE)
        context = browser.new_context(**kwargs)
        page = context.new_page()

        url = f"https://www.tradingview.com/symbols/{pair}/?exchange=FX_IDC"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            # Give the chart a moment to render.
            page.wait_for_timeout(3000)
            target = OUT_DIR / f"{pair}.png"
            page.screenshot(path=str(target), full_page=False)
            print(f"[tv_screenshot] saved {target}")
        except Exception as e:
            print(f"[tv_screenshot] failed for {pair}: {e}")
            return 1
        finally:
            context.close()
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
