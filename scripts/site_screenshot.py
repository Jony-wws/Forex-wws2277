"""Take a screenshot of the deployed FOREX dashboard via Playwright.

Used by .github/workflows/site_screenshot.yml every 5 hours to keep a
visual history of the live site under ``screenshots/site/<UTC>.png``.

Site URL is read from the SITE_URL env var (set in the workflow)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "screenshots" / "site"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> int:
    url = os.environ.get("SITE_URL")
    if not url:
        print("[site_screenshot] SITE_URL not set — skipping")
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[site_screenshot] playwright missing — skipping")
        return 0

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
    target = OUT_DIR / f"{ts}.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            viewport={"width": 412, "height": 915},
            is_mobile=True,
            device_scale_factor=2.625,
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; SM-A546B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
                "Mobile Safari/537.36"
            ),
        )
        page = ctx.new_page()
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=20000)
            # Let live data populate.
            page.wait_for_timeout(4000)
            page.screenshot(path=str(target), full_page=True)
            print(f"[site_screenshot] saved {target}")
        except Exception as e:
            print(f"[site_screenshot] failed: {e}")
            return 1
        finally:
            ctx.close()
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
