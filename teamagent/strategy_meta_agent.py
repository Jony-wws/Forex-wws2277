"""strategy_meta_agent — тактический мета-агент стратегии (5-часовой цикл).

Запускается как отдельный subprocess через orchestrator. Каждые 5 часов:

1. Тянет последние 5 дней 1h-баров Yahoo по всем 28 парам.
2. Для каждой (пара × сессия) ячейки прогоняет ВСЕ 120 strategy.VARIANTS
   на свежем 5-дневном окне и выбирает лучший variant (по WR + Wilson_lower).
3. Подмешивает ансамбль внешних сигналов (COT, fundamentals, regime, radar)
   как дополнительный score-shift и confidence-bonus.
4. Маркирует ячейку:
       QUALIFIED  — WR ≥ 70% И Wilson_lower ≥ 60% И trades ≥ 8
       PROBABLE   — 55% ≤ WR < 70%
       FROZEN     — иначе или мало данных
5. Пишет state/meta_strategy.json (potential gate-input для paper_trader)
   и state/meta_strategy_log.jsonl (live-лог последних N прогонов).
6. forecast_scanner читает meta_strategy.json и применяет
   `meta_strategy_bias` ±1..3 score-голос (см. forecast_scanner.evaluate_pair).

Это ТАКТИЧЕСКИЙ слой поверх 5-дневного strategy_search — он реактивен на
свежий рынок и обновляется в 24 раза чаще, но окно у него короче. Locked
365d-baseline остаётся "эталоном" — meta_agent не перезаписывает его.

Запуск:
    python -m teamagent.strategy_meta_agent          # один прогон, потом exit
    python -m teamagent.strategy_meta_agent --loop   # цикл: каждые 5 часов

Файлы:
    state/meta_strategy.json           — главный output (cells, summary)
    state/meta_strategy_log.jsonl      — лог прогонов (last 200 строк)
    state/heartbeat_strategy_meta_agent.json — heartbeat для watchdog
"""
from __future__ import annotations
import argparse
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from . import config, indicators, strategies
from .data import yahoo

log = logging.getLogger("strategy_meta_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "strategy_meta_agent.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

OUTPUT_FILE = config.STATE_DIR / "meta_strategy.json"
LOG_FILE = config.STATE_DIR / "meta_strategy_log.jsonl"
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_strategy_meta_agent.json"

# ───── параметры цикла ─────
LOOP_INTERVAL_SEC = 5 * 60 * 60          # 5 часов
LOOKBACK_DAYS = 5                        # окно бэктеста
MIN_TRADES_FOR_VALID = 8                 # минимум сделок чтобы ячейка считалась
QUALIFIED_WR_PCT = 70.0                  # минимум WR для QUALIFIED
QUALIFIED_WILSON_LOWER_PCT = 60.0        # минимум Wilson нижней границы
PROBABLE_WR_PCT = 55.0                   # граница PROBABLE/FROZEN
HEARTBEAT_INTERVAL_SEC = 60              # для watchdog
LOG_KEEP_LINES = 200                     # последние N строк log
PAYOUT_PCT = 0.85                        # бинарный payout (как в paper_trader)


def _heartbeat(tick: int = 0, status: str = "idle") -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "strategy_meta_agent",
        "category": "system",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "tick_count": tick,
        "status": status,
    }))


def _wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
    """Wilson lower 95% bound on win-rate (returns percentage 0-100)."""
    if total <= 0:
        return 0.0
    p = wins / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom) * 100.0


def _load_optional_signal(path: Path) -> dict:
    """Прочитать произвольный JSON-сигнал-файл; вернуть {} если нет/битый."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _ensemble_signals(pair: str) -> dict:
    """Собрать сигналы из COT / fundamentals / market_radar / market_regime
    для одной пары. Возвращаем dict с side_bias (-3..+3) и conf_bonus (0..15)."""
    bias = 0
    bonus = 0.0
    sources: list[dict] = []

    # COT contrarian (CFTC)
    try:
        from . import cot as cot_mod
        sig = cot_mod.pair_cot_signal(pair)
        if sig.get("side") in ("BUY", "SELL"):
            strength = float(sig.get("strength_pct", 0) or 0)
            pts = max(1, min(3, int(round(strength / 33))))
            if sig["side"] == "SELL":
                pts = -pts
            bias += pts
            bonus += min(5.0, abs(strength) / 20.0)
            sources.append({
                "name": "cot_contrarian",
                "side": sig["side"],
                "strength_pct": strength,
                "pts": pts,
            })
    except Exception:
        pass

    # Fundamentals (FRED rates / yields / cpi)
    try:
        from . import fundamentals as fund
        tilt = fund.pair_macro_tilt(pair)
        sc = float(tilt.get("tilt_score", 0) or 0)
        # ±20 raw → ±2 bias points
        pts = max(-2, min(2, int(round(sc / 10.0))))
        if pts != 0:
            bias += pts
            bonus += min(3.0, abs(sc) / 8.0)
            sources.append({
                "name": "fundamental_macro",
                "side": tilt.get("side"),
                "tilt_score": sc,
                "pts": pts,
            })
    except Exception:
        pass

    # market_radar (out of 20 sub-scanners)
    radar = _load_optional_signal(config.STATE_DIR / "market_radar.json")
    pair_radar = ((radar or {}).get("pairs") or {}).get(pair) or {}
    if pair_radar:
        score = float(pair_radar.get("composite_score") or pair_radar.get("score") or 0)
        if abs(score) >= 0.3:
            pts = max(-2, min(2, int(round(score * 4))))
            bias += pts
            bonus += min(3.0, abs(score) * 4)
            sources.append({
                "name": "market_radar",
                "score": round(score, 2),
                "pts": pts,
            })

    # market_regime_analyzer (regime confidence)
    regime = _load_optional_signal(config.STATE_DIR / "market_regime_365d.json")
    pair_regime = ((regime or {}).get("pairs") or {}).get(pair) or {}
    if pair_regime:
        rconf = float(pair_regime.get("confidence_pct") or 0)
        if rconf >= 60:
            bonus += min(4.0, (rconf - 60) / 10.0)
            sources.append({
                "name": "market_regime",
                "confidence_pct": rconf,
                "pts": 0,
            })

    return {
        "side_bias": int(bias),
        "confidence_bonus_pct": round(bonus, 1),
        "sources": sources,
    }


def _fetch_5d_snapshots(pair: str) -> Optional[list]:
    """Скачивает 5 дней 1h Yahoo + предрассчитывает индикаторы для каждого
    бара. Возвращает список (ts, close, ind_4h, ind_1h, ind_15m).

    Используем period="60d" для прогрева EMA200/RSI; бэктест-окно = последние
    LOOKBACK_DAYS дней."""
    bars = yahoo.fetch(pair, interval="1h", period="60d")
    if bars is None or bars.empty or len(bars) < 60:
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
        slice_15m = bars.iloc[max(0, idx - 100):idx]   # 15m proxy

        if len(slice_1h) < 30 or len(slice_4h) < 20 or len(slice_15m) < 30:
            snapshots.append((ts, close_p, None, None, None))
            continue
        ind_4h = indicators.all_indicators(slice_4h)
        ind_1h = indicators.all_indicators(slice_1h)
        ind_15m = indicators.all_indicators(slice_15m)
        if not ind_4h or not ind_1h or not ind_15m:
            snapshots.append((ts, close_p, None, None, None))
            continue
        snapshots.append((ts, close_p, ind_4h, ind_1h, ind_15m))
    return snapshots


def _walk_session(snapshots: list, strategy: strategies.Strategy,
                  session_window: tuple[int, int]) -> tuple[int, int, int]:
    """Прогоняет одну стратегию по снимкам, открывая сделки только когда
    ts.hour ∈ session_window. Возвращает (trades, wins, losses)."""
    open_trades: list[dict] = []
    wins = 0
    losses = 0
    sw_start, sw_end = session_window
    for ts, close_p, ind_4h, ind_1h, ind_15m in snapshots:
        # settle expired
        still = []
        for t in open_trades:
            if ts >= t["expiry"]:
                if t["side"] == "BUY":
                    if close_p > t["entry"]:
                        wins += 1
                    else:
                        losses += 1
                else:
                    if close_p < t["entry"]:
                        wins += 1
                    else:
                        losses += 1
            else:
                still.append(t)
        open_trades = still

        if ind_4h is None or ind_1h is None or ind_15m is None:
            continue
        # session filter (открываем только в окне)
        h = ts.hour
        if not (sw_start <= h < sw_end):
            continue
        # one-trade-per-pair-window guard
        if len(open_trades) >= 1:
            continue
        res = strategies.evaluate(strategy, ts, ind_4h, ind_1h, ind_15m)
        if res is None:
            continue
        side, _score, exp_h, _p = res
        open_trades.append({
            "side": side,
            "entry": close_p,
            "expiry": ts + timedelta(hours=exp_h),
        })
    # Force-close anything still open at the end
    if open_trades:
        last_ts = snapshots[-1][0]
        last_close = snapshots[-1][1]
        for t in open_trades:
            if last_ts >= t["expiry"] - timedelta(minutes=30):
                if t["side"] == "BUY":
                    if last_close > t["entry"]:
                        wins += 1
                    else:
                        losses += 1
                else:
                    if last_close < t["entry"]:
                        wins += 1
                    else:
                        losses += 1
    trades = wins + losses
    return trades, wins, losses


def _evaluate_cell(snapshots: list, session_name: str,
                   session_window: tuple[int, int]) -> dict:
    """Перебор 120 вариантов на одной (pair, session) ячейке. Возвращает
    лучший результат (по Wilson_lower с тай-брейком на trades)."""
    best: Optional[dict] = None
    for strat in strategies.VARIANTS:
        try:
            tr, w, l = _walk_session(snapshots, strat, session_window)
        except Exception:
            continue
        if tr < MIN_TRADES_FOR_VALID:
            continue
        wr = (w / tr * 100.0) if tr else 0.0
        wilson = _wilson_lower(w, tr)
        # tie-break: Wilson_lower → wr → trades
        score_key = (round(wilson, 2), round(wr, 2), tr)
        if best is None or score_key > best["_key"]:
            best = {
                "_key": score_key,
                "variant": strat.id,
                "variant_label": strat.label,
                "trades": tr,
                "wins": w,
                "losses": l,
                "win_rate_pct": round(wr, 1),
                "wilson_lower_pct": round(wilson, 1),
            }
    if best is None:
        return {
            "session": session_name,
            "session_window_utc": list(session_window),
            "status": "FROZEN",
            "reason": "no variant met MIN_TRADES_FOR_VALID",
            "trades": 0,
        }
    best.pop("_key", None)
    return {**best, "session": session_name, "session_window_utc": list(session_window)}


def evaluate_pair(pair: str) -> dict:
    """Полный анализ одной пары: 4 сессии × 120 вариантов на 5d окне."""
    t0 = time.time()
    snapshots = _fetch_5d_snapshots(pair)
    if snapshots is None:
        return {
            "pair": pair,
            "status": "NO_DATA",
            "by_session": {},
            "duration_sec": round(time.time() - t0, 1),
        }
    ensemble = _ensemble_signals(pair)
    by_session: dict[str, dict] = {}
    for sname, swin in strategies.SESSION_WINDOWS.items():
        cell = _evaluate_cell(snapshots, sname, swin)
        # apply ensemble side_bias / confidence_bonus
        if cell.get("trades", 0) >= MIN_TRADES_FOR_VALID:
            wr = cell.get("win_rate_pct", 0.0)
            wilson = cell.get("wilson_lower_pct", 0.0)
            # Confidence bonus pulls Wilson up by up to ensemble.bonus
            bonus = ensemble.get("confidence_bonus_pct", 0.0)
            adj_wilson = min(95.0, wilson + bonus)
            cell["wilson_adjusted_pct"] = round(adj_wilson, 1)
            # Side bias: if ensemble bias > 0 we expect BUY; if < 0 SELL
            cell["side_bias"] = ensemble["side_bias"]
            cell["ensemble_sources"] = ensemble["sources"]
            # Decide final status
            if wr >= QUALIFIED_WR_PCT and adj_wilson >= QUALIFIED_WILSON_LOWER_PCT:
                cell["status"] = "QUALIFIED"
            elif wr >= PROBABLE_WR_PCT:
                cell["status"] = "PROBABLE"
            else:
                cell["status"] = "FROZEN"
        else:
            cell["wilson_adjusted_pct"] = 0.0
            cell["side_bias"] = ensemble["side_bias"]
            cell["ensemble_sources"] = ensemble["sources"]
            cell["status"] = cell.get("status", "FROZEN")
        by_session[sname] = cell

    return {
        "pair": pair,
        "status": "OK",
        "by_session": by_session,
        "ensemble": ensemble,
        "duration_sec": round(time.time() - t0, 1),
    }


def run_full_sweep() -> dict:
    """Полный обход 28 пар × 4 сессии × 120 вариантов на 5d окне."""
    started = datetime.now(timezone.utc)
    log.info(f"meta-agent: starting sweep ({len(config.PAIRS)} pairs × 5d × 120 variants)")
    pair_results: dict[str, dict] = {}
    for i, pair in enumerate(config.PAIRS):
        _heartbeat(i + 1, status=f"sweep:{pair}")
        log.info(f"meta-agent: [{i+1}/{len(config.PAIRS)}] {pair}")
        try:
            pair_results[pair] = evaluate_pair(pair)
        except Exception as e:
            log.exception(f"meta-agent: {pair} failed: {e}")
            pair_results[pair] = {"pair": pair, "status": "ERROR", "error": str(e)}

    # ── агрегаты ──
    total_cells = 0
    qualified = 0
    probable = 0
    frozen = 0
    no_data = 0
    by_session_qual: dict[str, int] = {s: 0 for s in strategies.SESSION_WINDOWS}
    by_session_prob: dict[str, int] = {s: 0 for s in strategies.SESSION_WINDOWS}
    cells_flat: dict[str, dict] = {}
    sum_wr = 0.0
    cells_with_wr = 0

    for pair, pr in pair_results.items():
        if pr.get("status") != "OK":
            no_data += len(strategies.SESSION_WINDOWS)
            continue
        for sname, cell in (pr.get("by_session") or {}).items():
            total_cells += 1
            status = cell.get("status", "FROZEN")
            cell_id = f"{pair}:{sname}"
            cells_flat[cell_id] = {
                "pair": pair,
                "session": sname,
                "status": status,
                "win_rate_pct": cell.get("win_rate_pct"),
                "wilson_lower_pct": cell.get("wilson_lower_pct"),
                "wilson_adjusted_pct": cell.get("wilson_adjusted_pct"),
                "trades": cell.get("trades", 0),
                "wins": cell.get("wins"),
                "losses": cell.get("losses"),
                "variant": cell.get("variant"),
                "variant_label": cell.get("variant_label"),
                "side_bias": cell.get("side_bias", 0),
                "ensemble_sources": cell.get("ensemble_sources", []),
                "session_window_utc": cell.get("session_window_utc"),
            }
            if status == "QUALIFIED":
                qualified += 1
                by_session_qual[sname] = by_session_qual.get(sname, 0) + 1
            elif status == "PROBABLE":
                probable += 1
                by_session_prob[sname] = by_session_prob.get(sname, 0) + 1
            else:
                frozen += 1
            wr = cell.get("win_rate_pct")
            if wr is not None and cell.get("trades", 0) >= MIN_TRADES_FOR_VALID:
                sum_wr += wr
                cells_with_wr += 1

    expected_overall_wr = round(sum_wr / cells_with_wr, 1) if cells_with_wr else None

    finished = datetime.now(timezone.utc)
    duration_sec = round((finished - started).total_seconds(), 1)

    summary = {
        "as_of": finished.isoformat(),
        "started_at": started.isoformat(),
        "duration_sec": duration_sec,
        "lookback_days": LOOKBACK_DAYS,
        "cycle_seconds": LOOP_INTERVAL_SEC,
        "total_pairs": len(config.PAIRS),
        "total_cells": total_cells,
        "qualified": qualified,
        "probable": probable,
        "frozen": frozen,
        "no_data_cells": no_data,
        "by_session_qualified": by_session_qual,
        "by_session_probable": by_session_prob,
        "expected_overall_wr_pct": expected_overall_wr,
        "min_trades_for_valid": MIN_TRADES_FOR_VALID,
        "qualified_wr_threshold_pct": QUALIFIED_WR_PCT,
        "qualified_wilson_lower_pct": QUALIFIED_WILSON_LOWER_PCT,
    }

    out = {
        "as_of": finished.isoformat(),
        "summary": summary,
        "cells": cells_flat,
        "pairs": pair_results,
    }
    OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    _append_log({
        "ts": finished.isoformat(),
        "duration_sec": duration_sec,
        "qualified": qualified,
        "probable": probable,
        "frozen": frozen,
        "no_data_cells": no_data,
        "expected_overall_wr_pct": expected_overall_wr,
    })
    log.info(
        f"meta-agent: sweep done in {duration_sec}s — "
        f"qualified={qualified}/{total_cells} probable={probable} frozen={frozen}"
    )
    return out


def _append_log(entry: dict) -> None:
    """Дописывает строку в jsonl и обрезает файл до LOG_KEEP_LINES."""
    LOG_FILE.touch(exist_ok=True)
    lines: list[str] = []
    try:
        existing = LOG_FILE.read_text().splitlines()
        lines.extend(existing)
    except Exception:
        pass
    lines.append(json.dumps(entry, ensure_ascii=False))
    if len(lines) > LOG_KEEP_LINES:
        lines = lines[-LOG_KEEP_LINES:]
    LOG_FILE.write_text("\n".join(lines) + "\n")


def get_meta_strategy() -> dict:
    """Helper для других модулей: вернуть meta_strategy.json (или {})."""
    if not OUTPUT_FILE.exists():
        return {}
    try:
        return json.loads(OUTPUT_FILE.read_text())
    except Exception:
        return {}


def get_cell_for(pair: str, session: str) -> Optional[dict]:
    """Helper для forecast_scanner / paper_trader: вернуть конкретную ячейку."""
    data = get_meta_strategy()
    return (data.get("cells") or {}).get(f"{pair}:{session}")


_running = True


def _on_sig(_a, _b):
    global _running
    _running = False
    log.info("strategy_meta_agent: SIGTERM/SIGINT — stopping after current iter")


def _config_age_sec() -> float:
    if not OUTPUT_FILE.exists():
        return float("inf")
    return time.time() - OUTPUT_FILE.stat().st_mtime


def run_loop() -> None:
    """Цикл: при старте, если meta_strategy.json свежий (моложе ~LOOP-1ч) —
    пропускаем sweep и просто heartbeat. Иначе — sweep сразу. Далее sweep
    раз в LOOP_INTERVAL_SEC."""
    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)
    log.info("strategy_meta_agent: loop start")
    _heartbeat(0, status="boot")

    age = _config_age_sec()
    skip_threshold = LOOP_INTERVAL_SEC - 60 * 60   # 5h - 1h буфер
    if age < skip_threshold:
        log.info(f"strategy_meta_agent: meta_strategy.json fresh ({age/60:.0f} min old) — skipping initial sweep")
    else:
        try:
            run_full_sweep()
        except Exception as e:
            log.exception(f"strategy_meta_agent: initial sweep failed: {e}")

    next_run = time.time() + LOOP_INTERVAL_SEC
    tick = 1
    while _running:
        _heartbeat(tick, status="idle")
        if time.time() >= next_run:
            try:
                run_full_sweep()
            except Exception as e:
                log.exception(f"strategy_meta_agent: scheduled sweep failed: {e}")
            next_run = time.time() + LOOP_INTERVAL_SEC
        # heartbeat каждую минуту, чтобы watchdog видел жизнь
        for _ in range(HEARTBEAT_INTERVAL_SEC):
            if not _running:
                break
            time.sleep(1)
        tick += 1
    log.info("strategy_meta_agent: loop exit")


def _cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true", help="long-running loop (5h cycles)")
    args = p.parse_args()
    if args.loop:
        run_loop()
    else:
        run_full_sweep()


if __name__ == "__main__":
    _cli()
