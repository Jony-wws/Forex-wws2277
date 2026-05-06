"""TradingView auto-login via Playwright with Android Chrome user-agent.

Why mobile UA?  The user observed that TradingView's mobile login flow
does NOT trigger CAPTCHA on his Android Chrome.  We replicate that on
the GitHub Actions runner so this script can authenticate cleanly using
TV_USERNAME / TV_PASSWORD secrets without human intervention.

After login, the Playwright storage state (cookies + localStorage) is
saved to ``state/tv_storage.json`` so subsequent runs (e.g.
tv_screenshot.py) can reuse the session without re-logging in.

Run locally:
    pip install playwright && playwright install chromium
    TV_USERNAME=... TV_PASSWORD=... python scripts/tv_login.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "state"
STATE_DIR.mkdir(exist_ok=True)
STORAGE = STATE_DIR / "tv_storage.json"


ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-A546B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
    "Mobile Safari/537.36"
)


def main() -> int:
    user = os.environ.get("TV_USERNAME")
    pw   = os.environ.get("TV_PASSWORD")
    if not user or not pw:
        print("[tv_login] TV_USERNAME / TV_PASSWORD not set — skipping")
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[tv_login] playwright not installed — skipping")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ])
        context = browser.new_context(
            user_agent=ANDROID_UA,
            viewport={"width": 412, "height": 915},     # Pixel-class phone
            device_scale_factor=2.625,
            is_mobile=True,
            has_touch=True,
            locale="ru-RU",
        )
        page = context.new_page()

        try:
            page.goto("https://www.tradingview.com/accounts/signin/",
                      wait_until="domcontentloaded", timeout=30000)
            # Click "Email" button (first tab on the new mobile design).
            try:
                page.get_by_text("Email", exact=False).first.click(timeout=8000)
            except Exception:
                pass
            # Username + password.
            page.locator('input[name="id_username"], input[name="username"]').first.fill(user)
            page.locator('input[name="id_password"], input[name="password"]').first.fill(pw)
            # Submit.
            page.locator('button[type="submit"]').first.click()
            # Wait for nav after login.
            page.wait_for_load_state("networkidle", timeout=30000)
            # Verify we're logged in by hitting the user menu endpoint.
            page.goto("https://www.tradingview.com/", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            html = page.content()
            logged_in = ("/u/" in html or "user_menu" in html.lower()
                         or '"is_anonymous":false' in html)
            context.storage_state(path=str(STORAGE))
            print(f"[tv_login] storage saved to {STORAGE} "
                  f"(logged_in={logged_in})")
        except Exception as e:
            print(f"[tv_login] error: {e}")
            return 1
        finally:
            context.close()
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
