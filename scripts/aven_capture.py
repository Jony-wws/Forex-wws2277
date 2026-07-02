#!/usr/bin/env python3
"""
AVEN capture script: grabs live TradingView charts (M15/H1/H4) for a forex pair
via the public embed widget (no login needed), plus a ForexFactory economic
calendar snapshot and a ForexLive news snapshot. Saves PNGs under screenshots/.

Usage: python scripts/aven_capture.py AUDUSD
"""
import sys
import os
import asyncio
from playwright.async_api import async_playwright

TF = {"15M": "15", "1H": "60", "4H": "240"}


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


async def main():
    pair = (sys.argv[1] if len(sys.argv) > 1 else "AUDUSD").upper().replace("/", "")
    os.makedirs("screenshots/tv", exist_ok=True)
    os.makedirs("screenshots/news", exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        for label, iv in TF.items():
            url = (
                f"https://s.tradingview.com/widgetembed/?symbol=FX:{pair}"
                f"&interval={iv}&theme=dark&style=1"
            )
            await shoot(page, url, f"screenshots/tv/{pair}_{label}.png", wait_ms=8000)

        await shoot(
            page,
            "https://www.forexfactory.com/calendar",
            "screenshots/news/forexfactory_calendar.png",
            wait_ms=7000,
            full_page=True,
        )

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
