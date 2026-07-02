#!/usr/bin/env python3
"""
AVEN capture script.

Modes:
  python scripts/aven_capture.py AUDUSD   -> deep capture one pair (M15/H1/H4) + calendar + news
  python scripts/aven_capture.py ALL      -> scan mode: H4 chart for all 28 majors + calendar + news

Charts via public TradingView embed (no login). Calendar from TradingEconomics
(Investing.com fallback). News from ForexLive. Saves PNGs under screenshots/.
"""
import sys
import os
import asyncio
from playwright.async_api import async_playwright

TF = {"15M": "15", "1H": "60", "4H": "240"}

# 28 pairs from the 8 majors: USD EUR GBP JPY CHF AUD CAD NZD
ALL_PAIRS = [
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD",
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    "AUDJPY", "AUDCHF", "AUDCAD", "AUDNZD",
    "NZDJPY", "NZDCHF", "NZDCAD",
    "CADJPY", "CADCHF",
    "CHFJPY",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = { runtime: {} };
"""


async def shoot(page, url, path, wait_ms=6000, full_page=False):
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  ! goto failed for {url}: {e}")
    await page.wait_for_timeout(wait_ms)
    await page.screenshot(path=path, full_page=full_page)
    print(f"  saved {path}")


async def chart(page, pair, label, iv, folder):
    url = (
        f"https://s.tradingview.com/widgetembed/?symbol=FX:{pair}"
        f"&interval={iv}&theme=dark&style=1"
    )
    await shoot(page, url, f"{folder}/{pair}_{label}.png", wait_ms=7000)


async def try_calendar(page):
    sources = [
        ("https://tradingeconomics.com/calendar", "tradingeconomics"),
        ("https://www.investing.com/economic-calendar/", "investing"),
    ]
    for url, tag in sources:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(7000)
            body_text = (await page.inner_text("body"))[:2000].lower()
            if ("verify you are human" in body_text
                    or "performing security verification" in body_text
                    or "checking your browser" in body_text):
                print(f"  ! {tag} showed a bot-check page, trying next")
                continue
            await page.screenshot(
                path="screenshots/news/economic_calendar.png", full_page=True
            )
            print(f"  saved screenshots/news/economic_calendar.png (from {tag})")
            return True
        except Exception as e:
            print(f"  ! calendar {tag} failed: {e}")
    print("  ! all calendar sources failed")
    return False


async def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else "AUDUSD").upper().replace("/", "")
    os.makedirs("screenshots/tv", exist_ok=True)
    os.makedirs("screenshots/news", exist_ok=True)
    os.makedirs("screenshots/scan", exist_ok=True)

    scan_mode = arg in ("ALL", "SCAN")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            timezone_id="Europe/London",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()

        if scan_mode:
            # Overview H4 chart for every pair (trend screen)
            for i, pair in enumerate(ALL_PAIRS, 1):
                print(f"[{i}/{len(ALL_PAIRS)}] {pair}")
                await chart(page, pair, "4H", "240", "screenshots/scan")
        else:
            for label, iv in TF.items():
                await chart(page, arg, label, iv, "screenshots/tv")

        await try_calendar(page)

        await shoot(
            page,
            "https://www.forexlive.com/",
            "screenshots/news/forexlive.png",
            wait_ms=7000,
            full_page=True,
        )

        await browser.close()
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
</scan_capture_script.txt>