"""strategy_search — перебирает 30+ стратегий-вариантов на 30-дневной истории
для каждой пары и выбирает лучшую (по реальному win rate).

Оптимизация: индикаторы вычисляются ОДИН РАЗ для каждого идxа (4H/1H/15m),
затем все варианты прогоняются по уже посчитанным снимкам. Это в ~30 раз
быстрее наивного подхода.

Запуск:
    python -m teamagent.strategy_search           # один прогон
    python -m teamagent.strategy_search --top 5
    python -m teamagent.strategy_search --loop    # цикл: раз в 24 часа

Результат: state/strategy_config.json
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from . import config, indicators, strategies
from .data import yahoo

log = logging.getLogger("strategy_search")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "strategy_search.log"),
        logging.StreamHandler(sys.stdout),
    ],
)


OUTPUT_FILE = config.STATE_DIR / "strategy_config.json"
STAKE_USD = 50.0
PAYOUT_PCT = 0.85
MIN_TRADES_FOR_VALID = 5
LOOKBACK_DAYS = 30


def _precompute(pair: str) -> dict | None:
    """Вычислить (ts, close, ind_4h, ind_1h, ind_15m) для каждого валидного idx."""
    bars = yahoo.fetch(pair, interval="1h", period="3mo")
    if bars is None or bars.empty or len(bars) < 200:
        return None
    cutoff = bars.index[-1] - timedelta(days=LOOKBACK_DAYS)
    start_idx = bars.index.searchsorted(cutoff)
    if start_idx >= len(bars) - 5:
        return None
    bars_4h = bars.resample("4h").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna()

    snapshots = []
    for idx in range(start_idx, len(bars)):
        ts = bars.index[idx]
        close_p = float(bars.iloc[idx]["Close"])
        slice_1h = bars.iloc[max(0, idx - 100):idx]
        slice_4h = bars_4h.loc[bars_4h.index <= ts].tail(240)
        slice_15m = bars.iloc[max(0, idx - 100):idx]  # 15m proxy

        if len(slice_1h) < 30 or len(slice_4h) < 30 or len(slice_15m) < 30:
            snapshots.append((ts, close_p, None, None, None))
            continue

        ind_4h = indicators.all_indicators(slice_4h)
        ind_1h = indicators.all_indicators(slice_1h)
        ind_15m = indicators.all_indicators(slice_15m)
        if not ind_4h or not ind_1h or not ind_15m:
            snapshots.append((ts, close_p, None, None, None))
            continue

        snapshots.append((ts, close_p, ind_4h, ind_1h, ind_15m))

    return {"snapshots": snapshots}


def _walk_with_precomputed(snapshots: list, strategy: strategies.Strategy) -> tuple[int, int, int, float]:
    """Прогоняет стратегию по уже посчитанным снимкам."""
    open_trades: list[dict] = []
    wins = 0
    losses = 0
    pnl = 0.0

    for ts, close_p, ind_4h, ind_1h, ind_15m in snapshots:
        # settle expired
        still_open = []
        for t in open_trades:
            if ts >= t["expiry"]:
                if t["side"] == "BUY":
                    win = close_p > t["entry"]
                else:
                    win = close_p < t["entry"]
                if win:
                    wins += 1
                    pnl += STAKE_USD * PAYOUT_PCT
                else:
                    losses += 1
                    pnl -= STAKE_USD
            else:
                still_open.append(t)
        open_trades = still_open

        if open_trades:
            continue
        if ind_4h is None:
            continue

        out = strategies.evaluate(strategy, ts, ind_4h, ind_1h, ind_15m)
        if out is None:
            continue
        side, score, rec_h, prob = out

        open_trades.append({
            "side": side,
            "entry": close_p,
            "expiry": ts + timedelta(hours=rec_h),
        })

    total = wins + losses
    return total, wins, losses, pnl


def search_pair(pair: str, top: int = 5) -> dict:
    pre = _precompute(pair)
    if pre is None:
        return {
            "pair": pair,
            "best_variant": None,
            "best_label": None,
            "win_rate_pct": None,
            "trades": 0, "wins": 0, "losses": 0,
            "qualifies_70pct": False,
            "all_variants": [],
            "note": "insufficient history",
        }

    rows = []
    for v in strategies.VARIANTS:
        trades, wins, losses, pnl = _walk_with_precomputed(pre["snapshots"], v)
        wr = (wins / trades * 100.0) if trades > 0 else None
        rows.append({
            "variant": v.id,
            "label": v.label,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "pnl_usd": round(pnl, 2),
            "win_rate_pct": round(wr, 1) if wr is not None else None,
        })

    valid = [r for r in rows if r["trades"] >= MIN_TRADES_FOR_VALID and r["win_rate_pct"] is not None]
    valid.sort(key=lambda r: (-(r["win_rate_pct"] or 0), -r["trades"]))

    if not valid:
        return {
            "pair": pair,
            "best_variant": None,
            "best_label": None,
            "win_rate_pct": None,
            "trades": 0, "wins": 0, "losses": 0,
            "qualifies_70pct": False,
            "all_variants": rows,
            "note": "no variant produced ≥5 trades",
        }

    best = valid[0]
    return {
        "pair": pair,
        "best_variant": best["variant"],
        "best_label": best["label"],
        "win_rate_pct": best["win_rate_pct"],
        "trades": best["trades"],
        "wins": best["wins"],
        "losses": best["losses"],
        "pnl_usd": best["pnl_usd"],
        "qualifies_70pct": best["win_rate_pct"] >= 70.0,
        "all_variants": valid[:top],
    }


def search_all(top: int = 5) -> dict:
    results = {}
    for i, pair in enumerate(config.PAIRS, 1):
        log.info(f"[{i}/{len(config.PAIRS)}] strategy_search {pair} ...")
        try:
            r = search_pair(pair, top=top)
        except Exception as e:
            log.exception(f"[{i}/{len(config.PAIRS)}] {pair} failed: {e}")
            r = {
                "pair": pair, "best_variant": None, "best_label": None,
                "win_rate_pct": None, "trades": 0, "wins": 0, "losses": 0,
                "qualifies_70pct": False, "all_variants": [], "note": f"error: {e}",
            }
        results[pair] = r
        if r.get("best_variant"):
            log.info(f"  → best={r['best_variant']} WR={r['win_rate_pct']}% "
                     f"trades={r['trades']} qual70={r['qualifies_70pct']}")
        else:
            log.info(f"  → no valid variant ({r.get('note', '')})")

    qualified = [p for p, r in results.items() if r.get("qualifies_70pct")]
    summary = {
        "runs": len(config.PAIRS) * len(strategies.VARIANTS),
        "qualified_pairs_70pct": qualified,
        "qualified_count": len(qualified),
        "total_pairs": len(config.PAIRS),
    }

    by_variant: dict[str, list[float]] = {}
    by_variant_trades: dict[str, int] = {}
    by_variant_wins: dict[str, int] = {}
    for r in results.values():
        for av in r.get("all_variants", []):
            by_variant.setdefault(av["variant"], []).append(av["win_rate_pct"])
            by_variant_trades[av["variant"]] = by_variant_trades.get(av["variant"], 0) + av["trades"]
            by_variant_wins[av["variant"]] = by_variant_wins.get(av["variant"], 0) + av["wins"]
    global_means = []
    for vid, wrs in by_variant.items():
        if wrs:
            mean = sum(wrs) / len(wrs)
            agg_trades = by_variant_trades.get(vid, 0)
            agg_wins = by_variant_wins.get(vid, 0)
            agg_wr = (agg_wins / agg_trades * 100.0) if agg_trades else 0.0
            global_means.append({
                "variant": vid,
                "mean_wr_pct": round(mean, 1),
                "aggregated_wr_pct": round(agg_wr, 1),
                "pairs_with_data": len(wrs),
                "total_trades": agg_trades,
            })
    global_means.sort(key=lambda x: -x["aggregated_wr_pct"])
    summary["best_global_top10"] = global_means[:10]

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "pairs": results,
    }


HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_strategy_search.json"
LOOP_INTERVAL_SEC = 24 * 60 * 60   # раз в сутки
HEARTBEAT_INTERVAL_SEC = 60        # каждую минуту, чтобы watchdog видел


def _heartbeat(tick: int = 0) -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "strategy_search",
        "category": "system",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "tick_count": tick,
    }))


_running = True


def _on_sig(signum, frame):
    global _running
    _running = False
    log.info("strategy_search: SIGTERM/SIGINT — stopping")


def _do_one_run(top: int = 5) -> dict:
    log.info("strategy_search: starting full sweep")
    out = search_all(top=top)
    OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log.info(f"strategy_search done — wrote {OUTPUT_FILE} "
             f"(qualified={out['summary']['qualified_count']}/{out['summary']['total_pairs']})")
    return out


def run_loop(top: int = 5) -> None:
    """Цикл: первый прогон сразу, далее раз в сутки. Heartbeat каждую минуту."""
    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)
    log.info("strategy_search loop start")
    _heartbeat(0)
    last_run_at = 0.0
    tick = 0
    while _running:
        now = time.time()
        if now - last_run_at >= LOOP_INTERVAL_SEC or last_run_at == 0.0:
            try:
                _do_one_run(top=top)
            except Exception as e:
                log.exception(f"strategy_search run failed: {e}")
            last_run_at = time.time()
        tick += 1
        _heartbeat(tick)
        # сон с проверкой _running каждые HEARTBEAT_INTERVAL_SEC секунд
        slept = 0
        while _running and slept < HEARTBEAT_INTERVAL_SEC:
            time.sleep(1)
            slept += 1
    log.info("strategy_search loop exit")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5, help="how many top variants to keep per pair")
    ap.add_argument("--loop", action="store_true", help="run as a daemon (refresh every 24h)")
    args = ap.parse_args()

    if args.loop:
        run_loop(top=args.top)
    else:
        log.info("strategy_search start (one-shot)")
        _do_one_run(top=args.top)


if __name__ == "__main__":
    main()
