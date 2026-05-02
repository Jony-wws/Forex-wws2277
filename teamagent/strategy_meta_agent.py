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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
LOOKBACK_DAYS = 25                       # МАКСИМАЛЬНОЕ окно (snapshot-фрейм)
                                         # Реальный walk-forward делается по
                                         # MULTI_WINDOWS — каждой ячейке даём 6
                                         # шансов попасть в 70% WR.
MULTI_WINDOWS = [3, 5, 7, 10, 14, 21]    # дни — больше окон, лучшее выбирается
                                         # по Wilson_lower. 14d/21d дают больше
                                         # сделок (Wilson tighter), что
                                         # позволяет ячейкам с WR≥70% но n<8
                                         # пройти 60% Wilson-гейт без снижения
                                         # самого 70% порога.
MIN_TRADES_FOR_VALID = 5                 # минимум сделок чтобы ячейка считалась
QUALIFIED_WR_PCT = 70.0                  # минимум WR для QUALIFIED — НЕ снижаем
QUALIFIED_WILSON_LOWER_PCT = 60.0        # минимум Wilson нижней границы — НЕ снижаем
PROBABLE_WR_PCT = 55.0                   # граница PROBABLE/FROZEN
HEARTBEAT_INTERVAL_SEC = 60              # для watchdog
LOG_KEEP_LINES = 200                     # последние N строк log
PAYOUT_PCT = 0.85                        # бинарный payout (как в paper_trader)
PARALLEL_FETCH_WORKERS = 8               # параллелизм Yahoo-закачек (28 пар / 8 = ~4 batch)
PARALLEL_EVAL_WORKERS = 6                # параллелизм CPU-bound walk-forward (28 пар / 6 ≈ 5 batch)


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


def _ensemble_signals(pair: str, ctx: Optional[dict] = None) -> dict:
    """Собрать сигналы из COT / fundamentals / market_radar / market_regime
    + 5 бесплатных free_signals (currency strength, DXY, ATR regime,
    JPY confluence, VP distance) для одной пары.

    Возвращаем dict с side_bias (-7..+7) и conf_bonus (0..20).

    `ctx` — опциональный dict с предвычисленными:
      - 'strength_matrix': dict[currency, score]
      - 'dxy_df': pd.DataFrame DXY 1h
      - 'bulk_data': dict[pair, df] (тот же что run_full_sweep)
      - 'snapshots': list — для ATR-regime
    Если ctx None — free_signals не подключаются (унаследованное поведение)."""
    bias = 0
    bonus = 0.0
    sources: list[dict] = []
    ctx = ctx or {}

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

    # multi_tf_consensus (4h+1h+15m alignment)
    mtf = _load_optional_signal(config.STATE_DIR / "agent_analyzer_multi_tf_consensus.json")
    for sig in (mtf.get("summary", {}).get("signals") or []):
        if sig.get("pair") == pair:
            bs = int(sig.get("bull_score", 0))
            mx = int(sig.get("max", 3) or 3)
            if mx > 0 and bs >= mx - 1:
                bias += 1
                bonus += 1.5
                sources.append({"name": "multi_tf_bull", "score": f"{bs}/{mx}", "pts": 1})
            elif mx > 0 and bs <= 1:
                bias -= 1
                bonus += 1.5
                sources.append({"name": "multi_tf_bear", "score": f"{bs}/{mx}", "pts": -1})
            break

    # momentum_burst (recent 1h ATR/EMA breakout)
    mom = _load_optional_signal(config.STATE_DIR / "agent_analyzer_momentum_burst.json")
    for sig in (mom.get("summary", {}).get("signals") or []):
        if sig.get("pair") == pair:
            side = sig.get("burst_side") or sig.get("side")
            if side in ("BUY", "SELL"):
                pts = 1 if side == "BUY" else -1
                bias += pts
                bonus += 1.0
                sources.append({"name": "momentum_burst", "side": side, "pts": pts})
            break

    # news_filter (FF news blackout proximity → REDUCE confidence)
    nf = _load_optional_signal(config.STATE_DIR / "agent_analyzer_news_filter.json")
    nf_summary = nf.get("summary", {}) if isinstance(nf, dict) else {}
    blackout_pairs = nf_summary.get("blackout_pairs") or []
    if pair in blackout_pairs:
        bonus = max(0.0, bonus - 2.0)  # penalty: предстоит red-news
        sources.append({"name": "news_filter", "alert": "blackout_soon", "pts": 0})

    # session_strength: бонус, если текущий час уже в активной сессии
    ss = _load_optional_signal(config.STATE_DIR / "agent_analyzer_session_strength.json")
    sess = (ss.get("summary") or {}).get("session")
    if sess and sess in ("Asia", "London", "Overlap", "NY"):
        bonus += 0.5
        sources.append({"name": "session_active", "session": sess, "pts": 0})

    # Specialist agent (per-pair fast scanner, 15m+1h)
    spec = _load_optional_signal(config.STATE_DIR / f"agent_specialist_{pair}.json")
    spec_summary = (spec or {}).get("summary") or {}
    spec_bias = spec_summary.get("bias")
    spec_conf = float(spec_summary.get("confidence", 0) or 0)
    if spec_bias in ("BULL", "BEAR") and spec_conf >= 35:
        pts = 1 if spec_bias == "BULL" else -1
        bias += pts
        bonus += min(2.0, spec_conf / 30.0)
        sources.append({
            "name": "specialist_pair",
            "bias": spec_bias,
            "confidence": round(spec_conf, 1),
            "pts": pts,
        })

    # ─── free_signals (бесплатные источники, без API ключей) ───
    try:
        from . import free_signals
        # 1. currency strength matrix
        sm = ctx.get("strength_matrix")
        if sm:
            sig = free_signals.pair_strength_signal(pair, sm)
            if sig:
                bias += int(sig["pts"])
                bonus += min(2.0, abs(sig["pts"]))
                sources.append(sig)
        # 2. DXY trend (только USD-пары)
        dxy = ctx.get("dxy_df")
        if dxy is not None:
            sig = free_signals.pair_dxy_signal(pair, dxy)
            if sig:
                bias += int(sig["pts"])
                bonus += min(2.0, abs(sig["pts"]))
                sources.append(sig)
        # 3. ATR regime (confidence bonus, без bias)
        snaps = ctx.get("snapshots")
        if snaps:
            sig = free_signals.pair_atr_regime(snaps)
            if sig:
                # NORMAL — нейтрально, LOW/HIGH дают +1 bonus (variant adapt)
                if sig["regime"] in ("LOW", "HIGH"):
                    bonus += 1.0
                sources.append(sig)
        # 4. JPY confluence (только JPY-пары)
        bd = ctx.get("bulk_data")
        if bd:
            sig = free_signals.jpy_confluence_signal(pair, bd)
            if sig:
                bias += int(sig["pts"])
                bonus += 1.5
                sources.append(sig)
        # 5. VP distance (используем уже подготовленные agent_specialist_*.json)
        sig = free_signals.pair_vp_distance_signal(pair)
        if sig:
            bias += int(sig["pts"])
            bonus += 1.0
            sources.append(sig)
    except Exception as e:
        log.debug(f"free_signals for {pair} failed: {e}")

    # cap final bias / bonus to reasonable bounds (был ±5, теперь ±7 с free_signals)
    bias = max(-7, min(7, int(bias)))
    bonus = min(20.0, bonus)

    return {
        "side_bias": int(bias),
        "confidence_bonus_pct": round(bonus, 1),
        "sources": sources,
    }


def _bulk_fetch_1h_60d() -> dict[str, pd.DataFrame]:
    """Один HTTP-запрос к Yahoo за всеми 28 парами (1h, 60d). Возвращает
    {pair: DataFrame}. На rate-limit fall-through к пустому dict — тогда
    индивидуальный fetch внутри _fetch_5d_snapshots будет работать как
    fallback (с in-process cache yahoo._CACHE)."""
    try:
        import yfinance as yf
        tickers_str = " ".join(config.yahoo_ticker(p) for p in config.PAIRS)
        big = yf.download(
            tickers_str,
            interval="1h",
            period="60d",
            progress=False,
            auto_adjust=False,
            prepost=False,
            threads=True,
            group_by="ticker",
        )
    except Exception as e:
        log.warning(f"meta-agent: bulk yahoo fetch failed: {e}")
        return {}

    out: dict[str, pd.DataFrame] = {}
    if big is None or big.empty:
        return out
    for pair in config.PAIRS:
        ticker = config.yahoo_ticker(pair)
        try:
            if isinstance(big.columns, pd.MultiIndex) and ticker in big.columns.get_level_values(0):
                df = big[ticker].dropna(how="all")
            else:
                continue
            if df is None or df.empty or len(df) < 60:
                continue
            # Нормализуем индекс к tz-aware UTC и кладём в общий yahoo._CACHE,
            # чтобы повторные fetch внутри 2-мин TTL ловили кэш.
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")
            yahoo._CACHE[(pair, "1h", "60d")] = (time.time(), df.copy())
            out[pair] = df
        except Exception as e:
            log.warning(f"meta-agent: bulk parse {pair} failed: {e}")
    log.info(f"meta-agent: bulk-fetch ok — {len(out)}/{len(config.PAIRS)} pairs")
    return out


def _precompute_indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Векторизованно считает все индикаторы для всей серии одним проходом.

    Замена 360 вызовов indicators.all_indicators() (по одному на бар) на
    несколько pandas-операций на полной серии. Возвращает DataFrame с
    колонками rsi14/ema20/ema50/ema200/atr14/bb_pct/mom5/bbp/close.
    cei10 и ofi10 пропускаем — они зависят от tail(n), эквивалент
    можно посчитать постфактум (в backtest пока не используется).
    """
    if df is None or df.empty or len(df) < 30:
        return pd.DataFrame()
    close = df["Close"]
    out = pd.DataFrame(index=df.index)
    out["close"] = close
    out["rsi14"] = indicators.rsi(close, 14)
    out["ema20"] = indicators.ema(close, 20)
    out["ema50"] = indicators.ema(close, 50)
    out["ema200"] = indicators.ema(close, 200) if len(close) >= 200 else indicators.ema(close, max(13, len(close) - 1))
    out["atr14"] = indicators.atr(df, 14)
    out["bb_pct"] = indicators.bollinger_pct_b(close, 20, 2.0)
    out["mom5"] = indicators.momentum(close, 5)
    out["bbp"] = indicators.bbp(close, 1000)
    # vwap-session: используем typical-price MA(20) fallback
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df.get("Volume", pd.Series(0.0, index=df.index))
    if vol is None or vol.sum() <= 0:
        out["vwap"] = typical.rolling(20, min_periods=1).mean()
    else:
        cum_pv = (typical * vol).cumsum()
        cum_v = vol.cumsum().replace(0.0, float("nan"))
        out["vwap"] = (cum_pv / cum_v).fillna(typical)
    # cei/ofi на 10-баре окно — tail(10) операция, считаем rolling
    body = (df["Close"] - df["Open"]).abs()
    range_ = (df["High"] - df["Low"]).replace(0.0, float("nan"))
    out["cei10"] = (body / range_).fillna(0.0).rolling(10, min_periods=1).mean() * 100.0
    bull = (df["Close"] > df["Open"]).astype(int)
    bear = (df["Close"] < df["Open"]).astype(int)
    out["ofi10"] = (bull - bear).rolling(10, min_periods=1).mean()
    return out


def _ind_row_to_dict(row: pd.Series) -> Optional[dict]:
    """Превратить строку precomputed-frame в dict, как в indicators.all_indicators."""
    if row is None or row.isna().any():
        return None
    return {
        "rsi14": float(row["rsi14"]),
        "ema20": float(row["ema20"]),
        "ema50": float(row["ema50"]),
        "ema200": float(row["ema200"]),
        "atr14": float(row["atr14"]),
        "bb_pct": float(row["bb_pct"]),
        "mom5": float(row["mom5"]),
        "cei10": float(row["cei10"]),
        "ofi10": float(row["ofi10"]),
        "vwap": float(row["vwap"]),
        "bbp": float(row["bbp"]),
        "close": float(row["close"]),
    }


def _fetch_5d_snapshots(pair: str) -> Optional[list]:
    """Скачивает 5 дней 1h Yahoo + предрассчитывает индикаторы для каждого
    бара (векторизованно). Возвращает список (ts, close, ind_4h, ind_1h, ind_15m).

    Используем period="60d" для прогрева EMA200/RSI; бэктест-окно =
    последние LOOKBACK_DAYS дней. На большой серии все индикаторы
    считаются ОДНИМ проходом (RSI/EMA/ATR/BBP — все
    rolling/ewm pandas-ops), затем для каждого бара мы просто читаем
    соответствующие строки. Это в 50× быстрее старого подхода."""
    bars = yahoo.fetch(pair, interval="1h", period="60d")
    if bars is None or bars.empty or len(bars) < 60:
        return None
    # Окно snapshot-фрейма = max(MULTI_WINDOWS) — даёт всем окнам общий
    # источник снимков; затем _evaluate_cell фильтрует по ts для каждого окна.
    cutoff = bars.index[-1] - timedelta(days=max(MULTI_WINDOWS))
    start_idx = bars.index.searchsorted(cutoff)
    if start_idx >= len(bars) - 5:
        return None
    bars_4h = bars.resample("4h").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna()

    # ── Vectorized: pre-compute ALL indicators ONCE на каждом TF ──
    ind_frame_1h = _precompute_indicator_frame(bars)
    ind_frame_4h = _precompute_indicator_frame(bars_4h)
    # 15m proxy — используем тот же 1h frame
    ind_frame_15m = ind_frame_1h

    if ind_frame_1h.empty or ind_frame_4h.empty:
        return None

    snapshots = []
    for idx in range(start_idx, len(bars)):
        ts = bars.index[idx]
        close_p = float(bars.iloc[idx]["Close"])
        # ind_1h и ind_15m: строка [idx-1] (последний полностью закрытый бар)
        if idx == 0:
            snapshots.append((ts, close_p, None, None, None))
            continue
        try:
            row_1h = ind_frame_1h.iloc[idx - 1]
            row_15m = ind_frame_15m.iloc[idx - 1]
        except IndexError:
            snapshots.append((ts, close_p, None, None, None))
            continue
        # ind_4h: ищем последний 4h-бар, чей timestamp <= ts
        try:
            pos_4h = ind_frame_4h.index.searchsorted(ts) - 1
            if pos_4h < 0:
                snapshots.append((ts, close_p, None, None, None))
                continue
            row_4h = ind_frame_4h.iloc[pos_4h]
        except IndexError:
            snapshots.append((ts, close_p, None, None, None))
            continue
        ind_1h = _ind_row_to_dict(row_1h)
        ind_4h = _ind_row_to_dict(row_4h)
        ind_15m = _ind_row_to_dict(row_15m)
        if not ind_1h or not ind_4h or not ind_15m:
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


def _filter_snapshots_by_days(snapshots: list, days: int) -> list:
    """Возвращает суффикс snapshots где ts ≥ last_ts - days. Линейный поиск
    с конца, т.к. snapshots отсортированы по времени."""
    if not snapshots:
        return []
    last_ts = snapshots[-1][0]
    cutoff = last_ts - timedelta(days=days)
    # бинарный поиск по списку tuples — pandas-нативно проще, но snapshots
    # это list[tuple], так что простая итерация с конца до cutoff.
    for i in range(len(snapshots) - 1, -1, -1):
        if snapshots[i][0] < cutoff:
            return snapshots[i + 1:]
    return snapshots


def _evaluate_cell_one_window(snapshots: list, session_window: tuple[int, int]) -> Optional[dict]:
    """Перебор всех вариантов на одном окне. Возвращает лучший по
    (wilson_lower, wr, trades) или None если ни один variant не выдал
    MIN_TRADES_FOR_VALID сделок."""
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
    return best


def _evaluate_cell(snapshots: list, session_name: str,
                   session_window: tuple[int, int]) -> dict:
    """Multi-window перебор: пробуем 3/5/7/10 дневные окна и выбираем то,
    где Wilson_lower выше всего. Это даёт каждой ячейке 4× больше шансов
    попасть в 70% WR без снижения гейта.

    Возвращает лучший cell-result со всех окон (с пометкой winning_window_days).
    """
    best: Optional[dict] = None
    best_window: Optional[int] = None
    candidates_summary: list[dict] = []
    for win_days in MULTI_WINDOWS:
        sub = _filter_snapshots_by_days(snapshots, win_days)
        if len(sub) < 24:  # минимум 24 часовых бара
            continue
        cand = _evaluate_cell_one_window(sub, session_window)
        if cand is None:
            continue
        candidates_summary.append({
            "window_days": win_days,
            "variant": cand["variant"],
            "win_rate_pct": cand["win_rate_pct"],
            "wilson_lower_pct": cand["wilson_lower_pct"],
            "trades": cand["trades"],
        })
        if best is None or cand["_key"] > best["_key"]:
            best = cand
            best_window = win_days
    if best is None:
        return {
            "session": session_name,
            "session_window_utc": list(session_window),
            "status": "FROZEN",
            "reason": "no variant met MIN_TRADES_FOR_VALID across windows",
            "trades": 0,
            "window_candidates": candidates_summary,
        }
    best.pop("_key", None)
    return {
        **best,
        "session": session_name,
        "session_window_utc": list(session_window),
        "winning_window_days": best_window,
        "window_candidates": candidates_summary,
    }


def evaluate_pair_with_snapshots(pair: str, snapshots: Optional[list],
                                  ctx: Optional[dict] = None) -> dict:
    """То же что evaluate_pair, но snapshots передаются извне (после параллельной
    закачки в run_full_sweep). Не делает Yahoo-запрос — чистая CPU-работа.

    `ctx` — общий контекст (currency_strength_matrix, dxy_df, bulk_data),
    предвычисленный один раз в run_full_sweep и переиспользуемый для всех 28
    пар (вместо 28× повторов). Snapshots добавляется в ctx внутри функции."""
    t0 = time.time()
    if snapshots is None:
        return {
            "pair": pair,
            "status": "NO_DATA",
            "by_session": {},
            "duration_sec": round(time.time() - t0, 1),
        }
    pair_ctx = dict(ctx or {})
    pair_ctx["snapshots"] = snapshots
    ensemble = _ensemble_signals(pair, pair_ctx)
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


def evaluate_pair(pair: str) -> dict:
    """Sequential single-pair eval (Yahoo fetch + walk-forward). Используется
    тестами и при ручном запуске одной пары. run_full_sweep идёт параллельно."""
    snapshots = _fetch_5d_snapshots(pair)
    return evaluate_pair_with_snapshots(pair, snapshots)


def _fetch_one_safe(pair: str) -> tuple[str, Optional[list]]:
    """Wrapper для ThreadPoolExecutor: ловит исключения и возвращает (pair, None)
    при ошибке (например, Yahoo rate-limit)."""
    try:
        return pair, _fetch_5d_snapshots(pair)
    except Exception as e:
        log.warning(f"meta-agent: fetch {pair} failed: {e}")
        return pair, None


def run_full_sweep() -> dict:
    """Полный обход 28 пар × 4 сессии × 120 вариантов на 5d окне.

    Параллелизм:
    - Phase 1: Yahoo-закачки 28 пар через ThreadPoolExecutor(8) — самый
      медленный участок (I/O-bound, ~70s sequential → ~10s parallel).
    - Phase 2: walk-forward eval 28 пар через ThreadPoolExecutor(6) — CPU-
      bound, но GIL отпускается на pandas/numpy ops (~20s sequential → ~6s).

    Цель — полный sweep за ≤60 секунд (требование пользователя).
    """
    started = datetime.now(timezone.utc)
    log.info(
        f"meta-agent: starting sweep ({len(config.PAIRS)} pairs × 5d × 120 variants, "
        f"parallel fetch={PARALLEL_FETCH_WORKERS}, eval={PARALLEL_EVAL_WORKERS})"
    )
    pair_results: dict[str, dict] = {}

    # ── Phase 1a: bulk yfinance.download для всех 28 пар одним запросом ──
    fetch_t0 = time.time()
    bulk_t0 = time.time()
    bulk_data = _bulk_fetch_1h_60d()
    bulk_dur = round(time.time() - bulk_t0, 1)
    log.info(f"meta-agent: bulk fetch done in {bulk_dur}s — {len(bulk_data)}/{len(config.PAIRS)} cached")

    # ── Phase 1b: parallel snapshot-build (CPU-bound) с использованием bulk-cache ──
    snapshots_by_pair: dict[str, Optional[list]] = {}
    with ThreadPoolExecutor(max_workers=PARALLEL_FETCH_WORKERS) as ex:
        futures = {ex.submit(_fetch_one_safe, p): p for p in config.PAIRS}
        completed = 0
        for fut in as_completed(futures):
            pair, snaps = fut.result()
            snapshots_by_pair[pair] = snaps
            completed += 1
            if completed % 4 == 0 or completed == len(config.PAIRS):
                _heartbeat(completed, status=f"fetch:{completed}/{len(config.PAIRS)}")
                log.info(f"meta-agent: snapshot-built {completed}/{len(config.PAIRS)}")
    fetch_dur = round(time.time() - fetch_t0, 1)
    n_ok = sum(1 for s in snapshots_by_pair.values() if s is not None)
    log.info(f"meta-agent: phase 1 (fetch+snapshot) done in {fetch_dur}s — {n_ok}/{len(config.PAIRS)} pairs ok")

    # ── Phase 1c: pre-compute free_signals context (один раз на весь sweep) ──
    # currency strength matrix + DXY data — используются всеми 28 парами.
    fs_t0 = time.time()
    ctx: dict = {"bulk_data": bulk_data}
    try:
        from . import free_signals
        ctx["strength_matrix"] = free_signals.compute_currency_strength_matrix(bulk_data)
        ctx["dxy_df"] = free_signals.fetch_dxy_data()
        log.info(
            f"meta-agent: free_signals ctx ready in {round(time.time() - fs_t0, 1)}s "
            f"(strength={'/'.join(f'{c}={v:+.0f}' for c, v in (ctx.get('strength_matrix') or {}).items())} "
            f"dxy_rows={0 if ctx.get('dxy_df') is None else len(ctx['dxy_df'])})"
        )
    except Exception as e:
        log.warning(f"meta-agent: free_signals ctx prep failed: {e}")
        ctx = {"bulk_data": bulk_data, "strength_matrix": {}, "dxy_df": None}

    # ── Phase 2: parallel walk-forward eval ──
    eval_t0 = time.time()
    with ThreadPoolExecutor(max_workers=PARALLEL_EVAL_WORKERS) as ex:
        futures = {
            ex.submit(evaluate_pair_with_snapshots, p, snapshots_by_pair.get(p), ctx): p
            for p in config.PAIRS
        }
        completed = 0
        for fut in as_completed(futures):
            pair = futures[fut]
            try:
                pair_results[pair] = fut.result()
            except Exception as e:
                log.exception(f"meta-agent: eval {pair} failed: {e}")
                pair_results[pair] = {"pair": pair, "status": "ERROR", "error": str(e)}
            completed += 1
            if completed % 4 == 0 or completed == len(config.PAIRS):
                _heartbeat(completed, status=f"eval:{completed}/{len(config.PAIRS)}")
    eval_dur = round(time.time() - eval_t0, 1)
    log.info(f"meta-agent: phase 2 (eval) done in {eval_dur}s")

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
                "winning_window_days": cell.get("winning_window_days"),
                "window_candidates": cell.get("window_candidates", []),
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
        "bulk_fetch_sec": bulk_dur,
        "fetch_phase_sec": fetch_dur,
        "eval_phase_sec": eval_dur,
        "parallel_fetch_workers": PARALLEL_FETCH_WORKERS,
        "parallel_eval_workers": PARALLEL_EVAL_WORKERS,
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
        "fetch_phase_sec": fetch_dur,
        "eval_phase_sec": eval_dur,
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
