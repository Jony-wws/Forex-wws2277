"""playbook — per-(pair × session × regime) playbook builder.

Расширяет существующий strategy_search.json до 28 × 4 × 4 = 448 ячеек,
тегируя каждую историческую сделку лучшего variant'а из per-session top
текущим режимом (trending_up / trending_down / mean_reverting / chaotic).

Также вычисляет:
- worst-30-day rolling WR per (pair, session, regime) — для определения
  storm-proof статуса (резистентности к кризисам);
- Wilson lower bound 95% — нижняя граница доверительного интервала WR;
- side_bias — какую сторону (BUY/SELL) исторически предпочитает ячейка.

Запуск: python -m teamagent.playbook  → state/playbook.json

Используется:
- paper_trader.py: при выборе variant под текущий live regime;
- live_analyst.py: для генерации narrative «думаю в реальном времени»;
- /api/playbook: фронтенд показывает таблицу 28×4×4 с цветными маркерами.
"""
from __future__ import annotations
import argparse
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import config, indicators, regime, strategies
from .data import yahoo

log = logging.getLogger("playbook")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "playbook.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

OUTPUT_FILE = config.STATE_DIR / "playbook.json"
STAKE_USD = 50.0
PAYOUT_PCT = 0.85
MIN_TRADES_FOR_VALID = 5
LOOKBACK_DAYS = 365


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound (одно-сторонний нижний 95% CI), %."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - margin) / denom) * 100.0


def _precompute_with_regimes(pair: str, bars_1h: pd.DataFrame) -> dict | None:
    """Cache (ts, close, ind_4h, ind_1h, ind_15m, regime) per idx — раз на пару."""
    if bars_1h is None or bars_1h.empty or len(bars_1h) < 200:
        return None
    bars_4h = bars_1h.resample("4h").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna()

    cutoff = bars_1h.index[-1] - timedelta(days=LOOKBACK_DAYS)
    start_idx = bars_1h.index.searchsorted(cutoff)
    if start_idx >= len(bars_1h) - 5:
        return None

    REGIME_REFRESH_BARS = 6  # обновляем режим раз в 6 баров (= 6 часов 1H)
    snapshots = []
    cached_regime: str = "mean_reverting"
    for i, idx in enumerate(range(start_idx, len(bars_1h))):
        ts = bars_1h.index[idx]
        close_p = float(bars_1h.iloc[idx]["Close"])
        slice_1h = bars_1h.iloc[max(0, idx - 100):idx]
        slice_4h = bars_4h.loc[bars_4h.index <= ts].tail(240)
        if len(slice_1h) < 30 or len(slice_4h) < 30:
            snapshots.append((ts, close_p, None, None, None, cached_regime))
            continue
        ind_1h = indicators.all_indicators(slice_1h)
        ind_4h = indicators.all_indicators(slice_4h)
        ind_15m = ind_1h
        if not ind_1h or not ind_4h:
            snapshots.append((ts, close_p, None, None, None, cached_regime))
            continue
        if i % REGIME_REFRESH_BARS == 0:
            regime_window = bars_1h.iloc[max(0, idx - 200):idx]
            cached_regime = regime.classify_regime(regime_window, lookback=200)
        snapshots.append((ts, close_p, ind_4h, ind_1h, ind_15m, cached_regime))
    return {"snapshots": snapshots}


def _backtest_with_regime_cached(
    snapshots: list,
    variant_id: str,
    session_window: tuple[int, int],
) -> list[dict]:
    """Backtest вариант на уже посчитанных snapshots (с regime-тегом)."""
    variant_map = {s.id: s for s in strategies.VARIANTS}
    strategy = variant_map.get(variant_id)
    if strategy is None:
        return []

    trades_log: list[dict] = []
    open_trade: dict | None = None
    for ts, close_p, ind_4h, ind_1h, ind_15m, rgm in snapshots:
        if open_trade is not None and ts >= open_trade["expiry"]:
            if open_trade["side"] == "BUY":
                win = close_p > open_trade["entry"]
            else:
                win = close_p < open_trade["entry"]
            trades_log.append({
                "ts_open": open_trade["ts_open"].isoformat(),
                "ts_close": ts.isoformat(),
                "side": open_trade["side"],
                "regime": open_trade["regime"],
                "win": bool(win),
                "pnl": STAKE_USD * PAYOUT_PCT if win else -STAKE_USD,
                "hour_utc": int(open_trade["ts_open"].hour),
            })
            open_trade = None

        if open_trade is not None:
            continue
        if ind_4h is None:
            continue
        s, e = session_window
        if not (s <= ts.hour < e):
            continue

        out = strategies.evaluate(strategy, ts, ind_4h, ind_1h, ind_15m)
        if out is None:
            continue
        side, score, rec_h, prob = out

        open_trade = {
            "ts_open": ts,
            "side": side,
            "entry": close_p,
            "expiry": ts + timedelta(hours=rec_h),
            "regime": rgm,
        }

    return trades_log


def _aggregate_cells(
    pair: str,
    session: str,
    trades: list[dict],
) -> dict[str, dict]:
    """Группирует трейды по режиму и считает WR / Wilson / side_bias / storm-proof."""
    by_regime: dict[str, list[dict]] = {r: [] for r in regime.ALL_REGIMES}
    for t in trades:
        by_regime[t["regime"]].append(t)

    cells = {}
    for rgm, tlist in by_regime.items():
        n = len(tlist)
        wins = sum(1 for t in tlist if t["win"])
        wr = (wins / n * 100.0) if n > 0 else None
        wilson = wilson_lower(wins, n) if n > 0 else None
        n_buy = sum(1 for t in tlist if t["side"] == "BUY")
        n_sell = sum(1 for t in tlist if t["side"] == "SELL")
        if n_buy > n_sell * 1.2:
            side_bias = "BUY"
        elif n_sell > n_buy * 1.2:
            side_bias = "SELL"
        else:
            side_bias = "MIXED"

        # storm-proof: WR на ХУДШЕМ 30-дневном окне ≥ 55%
        worst_30d_wr = _worst_window_wr(tlist, window_days=30) if n >= 10 else None
        storm_proof = worst_30d_wr is not None and worst_30d_wr >= 55.0

        # Status
        if n < MIN_TRADES_FOR_VALID:
            status = "INSUFFICIENT"
        elif wr >= 70.0 and (wilson or 0) >= 55.0 and storm_proof:
            status = "STORM_PROOF"
        elif wr >= 70.0 and (wilson or 0) >= 55.0:
            status = "QUALIFIED"
        elif wr >= 60.0:
            status = "PROBABLE"
        else:
            status = "FROZEN"

        cells[rgm] = {
            "pair": pair,
            "session": session,
            "regime": rgm,
            "n_trades": n,
            "wins": wins,
            "losses": n - wins,
            "wr_pct": round(wr, 1) if wr is not None else None,
            "wilson_lower_pct": round(wilson, 1) if wilson is not None else None,
            "side_bias": side_bias,
            "n_buy": n_buy,
            "n_sell": n_sell,
            "worst_30d_wr_pct": round(worst_30d_wr, 1) if worst_30d_wr is not None else None,
            "storm_proof": storm_proof,
            "status": status,
        }
    return cells


def _worst_window_wr(trades: list[dict], window_days: int = 30) -> float | None:
    """Worst rolling-window WR (% wins) over `window_days`. None если мало данных."""
    if not trades:
        return None
    parsed = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(t["ts_open"])
            parsed.append((ts, 1 if t["win"] else 0))
        except Exception:
            pass
    if len(parsed) < 5:
        return None
    parsed.sort(key=lambda x: x[0])
    worst = 100.0
    for i in range(len(parsed)):
        ts_i = parsed[i][0]
        window_end = ts_i + timedelta(days=window_days)
        window = [w for ts, w in parsed[i:] if ts <= window_end]
        if len(window) < 3:
            continue
        wr = sum(window) / len(window) * 100.0
        if wr < worst:
            worst = wr
    return worst


def _select_top_variant_per_session(strategy_config: dict, pair: str, session: str) -> str | None:
    """Берёт лучший variant'a из существующего strategy_config для (pair, session)."""
    p = strategy_config.get("pairs", {}).get(pair, {})
    by_sess = p.get("by_session", {})
    sd = by_sess.get(session, {})
    return sd.get("best_variant")


def build_playbook(
    pair: str | None = None,
    bars_cache: dict | None = None,
    strategy_config: dict | None = None,
) -> dict:
    """Главный конструктор. Если pair=None — гоняет по всем 28 парам."""
    if strategy_config is None:
        try:
            strategy_config = json.loads(
                (config.STATE_DIR / "strategy_config.json").read_text()
            )
        except Exception:
            log.warning("strategy_config.json not found — playbook будет пустой")
            strategy_config = {"pairs": {}}

    pairs_to_process = [pair] if pair else list(config.PAIRS)

    out_pairs: dict[str, dict] = {}
    out_cells: list[dict] = []

    for i, p in enumerate(pairs_to_process, 1):
        log.info(f"[{i}/{len(pairs_to_process)}] playbook {p} ...")
        bars_1h = (bars_cache or {}).get(p)
        if bars_1h is None:
            try:
                bars_1h = yahoo.fetch(p, interval="1h", period="2y")
            except Exception as e:
                log.warning(f"  yahoo fail {p}: {e}")
                bars_1h = None
        if bars_1h is None or bars_1h.empty:
            out_pairs[p] = {"pair": p, "note": "no data", "sessions": {}}
            continue

        pre = _precompute_with_regimes(p, bars_1h)
        if pre is None:
            out_pairs[p] = {"pair": p, "note": "insufficient history", "sessions": {}}
            continue
        snapshots = pre["snapshots"]
        log.info(f"  pre {p}: {len(snapshots)} snapshots")

        sessions_summary: dict[str, dict] = {}
        for sess_name, sess_window in strategies.SESSION_WINDOWS.items():
            best_variant_id = _select_top_variant_per_session(strategy_config, p, sess_name)
            if best_variant_id is None:
                best_variant_id = "v04_prob75"

            trades = _backtest_with_regime_cached(snapshots, best_variant_id, sess_window)
            cells = _aggregate_cells(p, sess_name, trades)

            best_regime_status = "FROZEN"
            for rgm, c in cells.items():
                if c["status"] in {"STORM_PROOF", "QUALIFIED"}:
                    best_regime_status = "QUALIFIED"
                    break
                if c["status"] == "PROBABLE":
                    best_regime_status = "PROBABLE"

            sessions_summary[sess_name] = {
                "session": sess_name,
                "window_utc": list(sess_window),
                "best_variant": best_variant_id,
                "regimes": cells,
                "n_trades_total": sum(c["n_trades"] for c in cells.values()),
                "best_regime_status": best_regime_status,
            }
            for rgm, c in cells.items():
                c["best_variant"] = best_variant_id
                out_cells.append(c)

        n_q = sum(
            1 for s in sessions_summary.values()
            if s["best_regime_status"] in {"QUALIFIED"}
        )
        n_p = sum(
            1 for s in sessions_summary.values()
            if s["best_regime_status"] == "PROBABLE"
        )

        out_pairs[p] = {
            "pair": p,
            "sessions": sessions_summary,
            "n_sessions_qualified": n_q,
            "n_sessions_probable": n_p,
        }

    storm_proof = sum(1 for c in out_cells if c["status"] == "STORM_PROOF")
    qualified = sum(1 for c in out_cells if c["status"] == "QUALIFIED")
    probable = sum(1 for c in out_cells if c["status"] == "PROBABLE")
    frozen = sum(1 for c in out_cells if c["status"] == "FROZEN")
    insufficient = sum(1 for c in out_cells if c["status"] == "INSUFFICIENT")

    by_session_q: dict[str, int] = {s: 0 for s in strategies.SESSION_WINDOWS}
    by_regime_q: dict[str, int] = {r: 0 for r in regime.ALL_REGIMES}
    for c in out_cells:
        if c["status"] in {"STORM_PROOF", "QUALIFIED"}:
            by_session_q[c["session"]] = by_session_q.get(c["session"], 0) + 1
            by_regime_q[c["regime"]] = by_regime_q.get(c["regime"], 0) + 1

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_cells": len(out_cells),
            "storm_proof": storm_proof,
            "qualified": qualified,
            "probable": probable,
            "frozen": frozen,
            "insufficient": insufficient,
            "by_session_qualified": by_session_q,
            "by_regime_qualified": by_regime_q,
            "lookback_days": LOOKBACK_DAYS,
            "min_trades_for_valid": MIN_TRADES_FOR_VALID,
        },
        "cells": out_cells,
        "pairs": out_pairs,
    }


def run(pair: str | None = None) -> dict:
    pb = build_playbook(pair=pair)
    OUTPUT_FILE.write_text(json.dumps(pb, indent=2))
    log.info(
        f"playbook → {OUTPUT_FILE} | cells={pb['summary']['total_cells']}, "
        f"storm_proof={pb['summary']['storm_proof']}, "
        f"qualified={pb['summary']['qualified']}, "
        f"probable={pb['summary']['probable']}, "
        f"frozen={pb['summary']['frozen']}"
    )
    return pb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", help="single pair (debug)", default=None)
    args = ap.parse_args()
    run(pair=args.pair)
    return 0


if __name__ == "__main__":
    sys.exit(main())
