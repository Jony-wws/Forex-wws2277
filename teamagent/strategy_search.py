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
# Минимум сделок для того чтобы вариант считался "валидным" (статистически значимым).
# 5 сделок дают шум (4 win из 5 = 80%, но лотерея). 10+ — более стабильная оценка.
# В сегментации per-session это особенно важно: при 4 сессиях у каждой сессии
# в среднем 1/4 сделок от общего числа.
MIN_TRADES_FOR_VALID = 10
# История для бэктеста. Yahoo 1H держит ~730 дней. 180 дней (≈6 месяцев) —
# компромисс между repeatability (тренды могут меняться) и статистической
# стабильностью. С 180д на каждую (пара, сессия) ячейку приходится в среднем
# 180/4 = 45 рабочих часов сессии × ~25% сигналов = ~11 сделок (хватает чтобы
# больше вариантов проходили MIN_TRADES_FOR_VALID и реже теряли качественные
# ячейки из-за статистической недосигнальности).
LOOKBACK_DAYS = 90


def _precompute(pair: str) -> dict | None:
    """Вычислить (ts, close, ind_4h, ind_1h, ind_15m) для каждого валидного idx.

    Yahoo 1H interval поддерживает period до 2y. Используем 6mo чтобы иметь
    запас данных для индикаторов И LOOKBACK_DAYS=60 окна.
    """
    # 1y даёт ~365 дней 1H баров — нужно для LOOKBACK_DAYS=180 + ~30 дней
    # запаса для индикаторов (EMA, RSI, etc.).
    bars = yahoo.fetch(pair, interval="1h", period="1y")
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


def _walk_with_precomputed(
    snapshots: list,
    strategy: strategies.Strategy,
    session_window: tuple[int, int] | None = None,
) -> tuple[int, int, int, float]:
    """Прогоняет стратегию по уже посчитанным снимкам.

    session_window=(start_h, end_h) — если задан, открываем НОВЫЕ сделки
    только когда ts.hour ∈ [start, end). Закрытие истёкших сделок происходит
    в любой час (даже вне сессии — это просто settle на реальной цене Yahoo).
    """
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

        # session filter (per-session strategy_search)
        if session_window is not None:
            s, e = session_window
            h = ts.hour
            if not (s <= h < e):
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


def _eval_all_variants(snapshots: list, session_window: tuple[int, int] | None = None,
                        top: int = 5) -> tuple[dict | None, list[dict]]:
    """Прогоняет ВСЕ VARIANTS по snapshots (опционально с session filter).

    Возвращает (best_qualified_or_top, all_rows_sorted) где
    best — лучший вариант с trades≥MIN_TRADES_FOR_VALID и WR≥70 если есть,
           иначе вариант с лучшим WR (как best-effort), иначе None.
    """
    rows = []
    for v in strategies.VARIANTS:
        trades, wins, losses, pnl = _walk_with_precomputed(snapshots, v, session_window=session_window)
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
    # сортировка: ПЕРВЫЙ приоритет — WR ≥ 70, потом trades, потом WR
    valid.sort(key=lambda r: (
        0 if (r["win_rate_pct"] or 0) >= 70.0 else 1,   # qualifies first
        -(r["win_rate_pct"] or 0),                       # then highest WR
        -r["trades"],                                    # then most trades
    ))

    if not valid:
        return None, rows
    return valid[0], valid[:top]


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
            "by_session": {},
            "note": "insufficient history",
        }

    snapshots = pre["snapshots"]

    # 1) Лучшая стратегия по ВСЕЙ истории (как было раньше — для совместимости и фолбэка)
    best, valid_top = _eval_all_variants(snapshots, session_window=None, top=top)

    # 2) Лучшая стратегия для КАЖДОЙ из 4 канонических сессий
    by_session: dict[str, dict] = {}
    for sess_name, sess_window in strategies.SESSION_WINDOWS.items():
        sb, sb_top = _eval_all_variants(snapshots, session_window=sess_window, top=top)
        if sb is None:
            by_session[sess_name] = {
                "session": sess_name,
                "window_utc": list(sess_window),
                "best_variant": None,
                "best_label": None,
                "win_rate_pct": None,
                "trades": 0, "wins": 0, "losses": 0,
                "qualifies_70pct": False,
                "top_variants": [],
                "note": "no variant produced ≥5 trades in this session",
            }
        else:
            by_session[sess_name] = {
                "session": sess_name,
                "window_utc": list(sess_window),
                "best_variant": sb["variant"],
                "best_label": sb["label"],
                "win_rate_pct": sb["win_rate_pct"],
                "trades": sb["trades"],
                "wins": sb["wins"],
                "losses": sb["losses"],
                "pnl_usd": sb["pnl_usd"],
                "qualifies_70pct": (sb["win_rate_pct"] or 0) >= 70.0,
                "top_variants": sb_top,
            }

    if best is None:
        return {
            "pair": pair,
            "best_variant": None,
            "best_label": None,
            "win_rate_pct": None,
            "trades": 0, "wins": 0, "losses": 0,
            "qualifies_70pct": False,
            "all_variants": valid_top,
            "by_session": by_session,
            "note": "no variant produced ≥5 trades",
        }

    sessions_qualified = [s for s, d in by_session.items() if d.get("qualifies_70pct")]
    return {
        "pair": pair,
        "best_variant": best["variant"],
        "best_label": best["label"],
        "win_rate_pct": best["win_rate_pct"],
        "trades": best["trades"],
        "wins": best["wins"],
        "losses": best["losses"],
        "pnl_usd": best["pnl_usd"],
        "qualifies_70pct": (best["win_rate_pct"] or 0) >= 70.0,
        "all_variants": valid_top,
        "by_session": by_session,
        "sessions_qualified_70pct": sessions_qualified,
        "sessions_qualified_count": len(sessions_qualified),
    }


def search_all(top: int = 5) -> dict:
    results = {}
    for i, pair in enumerate(config.PAIRS, 1):
        # heartbeat per-pair чтобы watchdog не считал процесс мёртвым
        # во время длинного 30+ мин sweep с 120 вариантами × 180д.
        try:
            _heartbeat(tick=i)
        except Exception:
            pass
        log.info(f"[{i}/{len(config.PAIRS)}] strategy_search {pair} ...")
        try:
            r = search_pair(pair, top=top)
        except Exception as e:
            log.exception(f"[{i}/{len(config.PAIRS)}] {pair} failed: {e}")
            r = {
                "pair": pair, "best_variant": None, "best_label": None,
                "win_rate_pct": None, "trades": 0, "wins": 0, "losses": 0,
                "qualifies_70pct": False, "all_variants": [],
                "by_session": {}, "note": f"error: {e}",
            }
        results[pair] = r
        if r.get("best_variant"):
            sess_q = r.get("sessions_qualified_70pct", [])
            log.info(
                f"  → best={r['best_variant']} WR={r['win_rate_pct']}% "
                f"trades={r['trades']} qual70={r['qualifies_70pct']} "
                f"sessions_qual({len(sess_q)}/4)={sess_q}"
            )
        else:
            log.info(f"  → no valid variant ({r.get('note', '')})")

    qualified = [p for p, r in results.items() if r.get("qualifies_70pct")]
    summary = {
        "runs": len(config.PAIRS) * len(strategies.VARIANTS),
        "runs_per_session": len(config.PAIRS) * len(strategies.VARIANTS) * len(strategies.SESSION_WINDOWS),
        "qualified_pairs_70pct": qualified,
        "qualified_count": len(qualified),
        "total_pairs": len(config.PAIRS),
        "session_windows_utc": {k: list(v) for k, v in strategies.SESSION_WINDOWS.items()},
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

    # ─── per-session summary ───
    session_summary: dict[str, dict] = {}
    for sess_name in strategies.SESSION_WINDOWS:
        qual_pairs = []
        total_pairs_with_data = 0
        wr_values: list[float] = []
        trades_total = 0
        wins_total = 0
        for pair, r in results.items():
            sd = (r.get("by_session") or {}).get(sess_name) or {}
            wr = sd.get("win_rate_pct")
            if wr is None:
                continue
            total_pairs_with_data += 1
            wr_values.append(wr)
            trades_total += sd.get("trades") or 0
            wins_total += sd.get("wins") or 0
            if sd.get("qualifies_70pct"):
                qual_pairs.append(pair)
        agg_wr = (wins_total / trades_total * 100.0) if trades_total else None
        session_summary[sess_name] = {
            "session": sess_name,
            "window_utc": list(strategies.SESSION_WINDOWS[sess_name]),
            "qualified_pairs_70pct": qual_pairs,
            "qualified_count": len(qual_pairs),
            "total_pairs_with_data": total_pairs_with_data,
            "mean_wr_pct": round(sum(wr_values) / len(wr_values), 1) if wr_values else None,
            "aggregated_wr_pct": round(agg_wr, 1) if agg_wr is not None else None,
            "trades_total": trades_total,
            "wins_total": wins_total,
        }
    summary["by_session"] = session_summary

    # сколько в среднем сделок проходит per-session гейт за день (грубая оценка)
    avg_trades_per_day_session = sum(
        (s["trades_total"] / 30.0) for s in session_summary.values() if s["trades_total"]
    )
    summary["est_trades_per_day_via_session_gate"] = round(avg_trades_per_day_session, 1)

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
