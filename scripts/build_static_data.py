"""Build static JSON snapshot files used by the GitHub Pages dashboard.

Run by `.github/workflows/refresh_data.yml` every 15 minutes on a fresh
ubuntu-latest runner with real internet access — so yfinance actually
works and we genuinely get live Yahoo Finance data.

Output layout (relative to repo root):

    data/
        signals.json            # all 28 pairs with signals/forecasts
        cycle.json              # strict 5h-cycle snapshot
        orderbooks.json         # {PAIR: OrderBook} for all pairs
        health.json             # timestamps + diagnostics
        bars/EURUSD-1h.json     # per-pair per-interval candle data
        bars/EURUSD-4h.json
        ...                     # (28 pairs × 4 intervals = 112 files)

Downstream, the React SPA at jony-wws.github.io/Forex-wws2277 fetches
these files directly from raw.githubusercontent.com/.../data branch.

The script is intentionally tolerant of per-pair failures: one dead
ticker must not stop the cron from writing a fresh snapshot of the
other 27.  Fatal errors (e.g. Yahoo Finance completely unreachable)
still raise so the workflow turns red and we get a notification.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Make `app` importable when script is run directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analyzer import analyze_pair  # noqa: E402
from app.config import MIN_CONFIDENCE, PAIRS, PAIR_NAMES_RU, TZ_UTC5  # noqa: E402
from app.orderbook import get_orderbook  # noqa: E402
from app.prices import fetch_bars, get_current_price, get_price_change  # noqa: E402
from app import cycle as cycle_mod  # noqa: E402

log = logging.getLogger("build_static_data")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# Mirror `BAR_INTERVALS` in app/main.py so /v2 and /pages behave the
# same (chart picker shows the same four buckets).
BAR_INTERVALS: dict[str, str] = {
    "15m": "5d",
    "1h": "1mo",
    "4h": "3mo",
    "1d": "1y",
}

DATA_DIR = ROOT / "data"
BARS_DIR = DATA_DIR / "bars"


def _build_pair_entry(pair: str) -> dict | None:
    """Mirror of app/main.py::_build_entry — kept here to avoid having
    to import a FastAPI-bound helper just to get the shape right."""
    price = get_current_price(pair)
    if price is None:
        return None

    analysis = analyze_pair(pair)
    price_info = get_price_change(pair)

    is_jpy = "JPY" in pair
    pip_mult = 100 if is_jpy else 10000

    entry: dict = {
        "pair": pair,
        "name_ru": PAIR_NAMES_RU.get(pair, pair),
        "price": price,
        "price_display": f"{price:.3f}" if is_jpy else f"{price:.5f}",
        "change_24h_pips": (
            round(price_info["change"] * pip_mult, 1) if price_info else 0
        ),
        "change_24h_pct": (price_info["change_pct"] if price_info else 0),
    }

    if analysis:
        has_signal = (
            analysis["side"] is not None
            and analysis["confidence"] >= MIN_CONFIDENCE
        )
        entry.update(
            signal=analysis["side"] if has_signal else None,
            side=analysis["side"],
            confidence=analysis["confidence"],
            strength=(
                analysis["strength"] if has_signal else "Нет сигнала"
            ),
            score=analysis["score"],
            max_score=analysis.get("max_score", 0),
            multi_tf_aligned=bool(analysis.get("multi_tf_aligned")),
            adx_h1=analysis.get("adx_h1", 0.0),
            adx_h4=analysis.get("adx_h4", 0.0),
            trend_persistence_5h=analysis.get("trend_persistence_5h", 0.0),
            trend_persistence_bars=analysis.get("trend_persistence_bars", 0),
            is_strong_trend=bool(analysis.get("is_strong_trend")),
            details=analysis["details"],
            indicators=analysis["indicators"],
            forecast_5h=analysis["forecast_5h"],
            forecast_24h=analysis["forecast_24h"],
        )
    else:
        entry.update(
            signal=None,
            side=None,
            confidence=0,
            strength="Нет данных",
            score=0,
            max_score=0,
            multi_tf_aligned=False,
            adx_h1=0.0,
            adx_h4=0.0,
            trend_persistence_5h=0.0,
            trend_persistence_bars=0,
            is_strong_trend=False,
            details=[],
            indicators={},
            forecast_5h=None,
            forecast_24h=None,
        )

    return entry


def _bars_to_list(df) -> list[dict]:
    """DataFrame → lightweight-charts-ready payload. Same transform as
    the /api/bars endpoint in app/main.py (kept in sync by hand)."""
    if df is None or df.empty:
        return []
    out: list[dict] = []
    for ts, row in df.iterrows():
        try:
            close = float(row["Close"])
        except (TypeError, ValueError):
            continue
        if close != close:  # NaN guard — yfinance occasionally emits NaN bars
            continue
        out.append({
            "time": int(ts.timestamp()),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": close,
            "volume": float(row.get("Volume", 0) or 0),
        })
    return out


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # default=str covers pandas Timestamp and any stray datetime objects.
    path.write_text(
        json.dumps(payload, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def build(*, include_bars: bool) -> dict:
    """Run the pipeline once and write all output files.

    Returns a small summary dict suitable for use as the GitHub Actions
    job summary."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BARS_DIR.mkdir(parents=True, exist_ok=True)

    started = time.time()
    pair_errors: list[str] = []
    built_pairs: dict[str, dict] = {}
    orderbooks: dict[str, dict] = {}

    for pair in PAIRS:
        try:
            entry = _build_pair_entry(pair)
            if entry:
                built_pairs[pair] = entry
        except Exception as exc:
            log.exception("pair %s failed: %s", pair, exc)
            pair_errors.append(f"{pair}: {exc}")

    for pair in PAIRS:
        try:
            orderbooks[pair] = get_orderbook(pair)
        except Exception as exc:
            log.warning("orderbook %s failed: %s", pair, exc)

    # 5h-cycle state.  Load persisted state from disk so winrate and
    # history accumulate across runs (same `state/` dir that the Fly
    # container used).
    try:
        cycle_mod.init()
        cycle_mod.tick(built_pairs)
        cycle_snapshot = cycle_mod.snapshot()
    except Exception as exc:
        log.exception("cycle tick failed: %s", exc)
        cycle_snapshot = {
            "current_cycle": None,
            "next_cycle_utc": "",
            "seconds_to_next_cycle": 0,
            "winrate_5h": {"wins": 0, "losses": 0, "decisions": 0,
                           "winrate_pct": 0, "cycles": 0},
            "winrate_24h": {"wins": 0, "losses": 0, "decisions": 0,
                            "winrate_pct": 0, "cycles": 0},
            "history_cycles": 0,
            "win_threshold_pct": 0.10,
            "min_picks": 3,
            "max_picks": 5,
            "strong_gate": {},
        }

    now_utc5 = datetime.now(TZ_UTC5).strftime("%Y-%m-%d %H:%M:%S")
    signals_payload = {
        "pairs": built_pairs,
        "updated_at": now_utc5,
        "scan_count": int(time.time()) // 60,  # minute-bucket counter
    }

    _write_json(DATA_DIR / "signals.json", signals_payload)
    _write_json(DATA_DIR / "cycle.json", cycle_snapshot)
    _write_json(DATA_DIR / "orderbooks.json", orderbooks)

    bar_files = 0
    if include_bars:
        for pair in PAIRS:
            for interval, period in BAR_INTERVALS.items():
                try:
                    df = fetch_bars(pair, interval=interval, period=period)
                    bars = _bars_to_list(df)
                except Exception as exc:
                    log.warning("bars %s %s failed: %s", pair, interval, exc)
                    bars = []
                _write_json(
                    BARS_DIR / f"{pair}-{interval}.json",
                    {"pair": pair, "interval": interval, "bars": bars},
                )
                bar_files += 1

    elapsed = round(time.time() - started, 1)
    health = {
        "status": "ok" if built_pairs else "empty",
        "updated_at_utc5": now_utc5,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pairs_built": len(built_pairs),
        "pairs_total": len(PAIRS),
        "orderbooks_built": len(orderbooks),
        "bar_files": bar_files,
        "elapsed_seconds": elapsed,
        "errors": pair_errors,
    }
    _write_json(DATA_DIR / "health.json", health)

    log.info(
        "Done in %.1fs — %d/%d pairs, %d orderbooks, %d bar files, %d errors",
        elapsed, len(built_pairs), len(PAIRS),
        len(orderbooks), bar_files, len(pair_errors),
    )
    return health


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-bars",
        action="store_true",
        help="Skip regenerating per-pair candle files (faster smoke run).",
    )
    args = parser.parse_args()

    try:
        health = build(include_bars=not args.no_bars)
    except Exception:
        traceback.print_exc()
        return 1

    # When run under GitHub Actions emit a pretty job summary.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## FOREX data refresh\n\n")
            fh.write(f"- Built **{health['pairs_built']}/{health['pairs_total']}** pairs\n")
            fh.write(f"- Orderbooks: {health['orderbooks_built']}\n")
            fh.write(f"- Bar files: {health['bar_files']}\n")
            fh.write(f"- Elapsed: {health['elapsed_seconds']}s\n")
            if health["errors"]:
                fh.write("\n### Per-pair errors\n\n")
                for err in health["errors"]:
                    fh.write(f"- {err}\n")

    return 0 if health["pairs_built"] > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
