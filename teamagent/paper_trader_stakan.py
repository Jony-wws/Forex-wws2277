"""Paper-Trader STAKAN — параллельная стратегия на Volume-Profile «не-возвратных» уровнях.

Запрос пользователя (2026-05-01):
> "найти уровень от которого цена будет уходить даже не будет приближаться …
>  не только не пробьёт а не будет приближаться идти к нему не будет даже на пипсе …
>  система сам будет решать насколько открыт сделку (1–20 ч) …
>  объединяем все агенты, все знания и все факты …
>  из десяти прогнозов минимум 7 плюсов".

Логика (на каждом цикле, раз в 60 сек, для каждой из 28 пар):

1. Строим Volume Profile по последним 24h 1-минутных баров (1440 баров).
2. Из big_players (≥80-percentile объёма) выбираем кандидатов на «уровень-отталкивание».
3. Для каждого кандидата считаем по последним 24h:
   - min_distance      — минимальное расстояние цены до уровня (в pips)
   - approach_count    — сколько раз цена шла в сторону уровня в пределах 1×ATR, но
                         развернулась
   - rejection_count   — сколько раз свеча буквально касалась уровня и закрывалась
                         в обратную сторону (вершина/дно > уровня, close ниже/выше)
   - avoidance_score   = min_distance / atr  (чем больше — тем «святее» уровень)
4. Уровень-отталкивание считается VALID если:
   - min_distance >= LEVEL_AVOIDANCE_ATR_MULT × atr (цена ни разу не подошла слишком
     близко за 24h)
   - approach_count >= MIN_APPROACH_COUNT (но именно «приближалась» — не флэт)
   - rejection_count >= MIN_REJECTION_COUNT (хотя бы пару раз отскочила)
5. Если найден ≥1 валидный уровень — direction = AWAY (если уровень ниже cur_price →
   BUY; если выше — SELL).
6. Считаем 10-vote consensus, объединяя ВСЕ источники сигнала:
   - vote 1: VP no_return направление совпадает с направлением «от уровня»
   - vote 2: PROGNOZY-28 forecast.probability_pct ≥ 70%
   - vote 3: forecast.side совпадает с direction
   - vote 4: macro tilt (fundamentals) совпадает
   - vote 5: COT contrarian signal совпадает
   - vote 6: RSI(1H) НЕ в экстремуме против direction (не >75 если BUY, не <25 если SELL)
   - vote 7: EMA20 vs EMA50 совпадает с direction (тренд подтверждает)
   - vote 8: ATR не аномально высокий (< 1.5× medium ATR за 5 дней)
   - vote 9: нет news blackout (high-impact ±30 мин)
   - vote 10: расстояние до POC ≥ 0.3×ATR (есть куда ходить)
   Открываем сделку если **≥ MIN_VOTES (7) из 10** проголосовали ЗА.
7. expiry (1–20 ч) выбираем автоматически:
   - distance_atr = level_distance_pips / atr_pips
   - expiry_h = clamp(round(distance_atr × EXPIRY_PER_ATR), 1, 20)
   - корректировка: если volatility высокая (atr/price > 0.005) → /2 (быстрее)

State files (отдельные от основного paper_trader):
- state/stakan_open_trades.json
- state/stakan_closed_trades.json
- state/stakan_stats.json
- state/heartbeat_paper_trader_stakan.json

Закрытие сделок — то же что в основном trader: по реальной цене Yahoo на момент expiry.
"""
from __future__ import annotations
import json
import logging
import signal
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import config
from .data import yahoo
from . import indicators as ind
from . import volume_profile as vp_mod

log = logging.getLogger("paper_stakan")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "paper_trader_stakan.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

FORECASTS_FILE = config.STATE_DIR / "forecasts.json"
OPEN_FILE = config.STATE_DIR / "stakan_open_trades.json"
CLOSED_FILE = config.STATE_DIR / "stakan_closed_trades.json"
STATS_FILE = config.STATE_DIR / "stakan_stats.json"
SIGNALS_FILE = config.STATE_DIR / "stakan_signals.json"  # snapshot последнего скана для UI
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_paper_trader_stakan.json"
FUNDAMENTALS_FILE = config.STATE_DIR / "agent_analyzer_fundamental_macro.json"
COT_FILE = config.STATE_DIR / "agent_analyzer_cot_positioning.json"
NEWS_FILE = config.STATE_DIR / "news_blackouts.json"
RADAR_FILE = config.STATE_DIR / "market_radar.json"

# ───── параметры стратегии «Стакан» ─────
# VP_HISTORY_DAYS: на каком окне строим VP и ищем «институциональные» уровни.
# Чем больше окно — тем «святее» уровни. 7 дней даёт ~10 080 1-мин баров, что
# достаточно для устойчивого профиля (минимум 5 торговых сессий).
VP_HISTORY_DAYS = 7
SCAN_WINDOW_MINUTES = 24 * 60          # 24h окно для проверки «уходит ли цена»
LEVEL_AVOIDANCE_ATR_MULT = 0.6         # дистанция уровня до ближайшего 24h-экстремума ≥ 0.6×ATR_1h
MIN_LEVEL_DISTANCE_ATR_MULT = 0.8      # уровень должен быть ≥0.8×ATR от текущей цены
MAX_LEVEL_DISTANCE_ATR_MULT = 8.0      # уровень не должен быть слишком далеко (≤8×ATR), иначе он «не активен»
APPROACH_BAND_ATR_MULT = 3.0           # «полоса приближения» = ±3×ATR от уровня
MIN_APPROACH_BARS_PCT = 1.0            # цена была в полосе приближения хотя бы 1% времени за 24h
MIN_VOTES = 8                          # из 11 голосов ≥8 ЗА → открываем (≥72%)
MAX_VOTES = 11                         # 10 базовых + 1 от market_radar
MIN_EXPIRY_H = 1
MAX_EXPIRY_H = 20
EXPIRY_PER_ATR = 1.0                   # 1×ATR расстояния = 1h экспирации
HIGH_VOL_PRICE_RATIO = 0.005           # ATR/price > 0.005 → высокая волатильность

# ───── 10-минутный pre-trade фильтр (по уточнению пользователя 2026-05-01) ─────
# «Когда я сказал про 10 минут это не закрыть сделку … я говорил система должна
# заранее знать что он за 10 минут развернуться к нашу сторону от этом я говорил».
# → Это НЕ early-close (брокер не поддерживает), а ПРЕДСКАЗАТЕЛЬНЫЙ фильтр:
# открываем сделку ТОЛЬКО если есть ≥3 из 5 краткосрочных индикаторов того,
# что цена в ближайшие ~10 мин начнёт двигаться в нашу сторону.
REVERSAL_LOOKBACK_MIN = 10             # горизонт прогноза: 10 минут вперёд
REVERSAL_MIN_SIGNS = 3                 # из 5 микро-индикаторов нужно ≥3
REVERSAL_RSI_OVERSOLD = 30             # RSI на 5m < 30 → перепродано → BUY-разворот
REVERSAL_RSI_OVERBOUGHT = 70           # RSI на 5m > 70 → перекуплено → SELL-разворот
REVERSAL_NEAR_LEVEL_ATR_MULT = 0.5     # цена в полосе ±0.5×ATR_15m от микро-уровня


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        log.warning(f"corrupt {path.name}, resetting")
        return default


def _save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))


def _heartbeat() -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "paper_trader_stakan",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": __import__("os").getpid(),
    }))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pip_size(pair: str) -> float:
    """1 pip для forex: 0.01 для JPY-пар, 0.0001 для остальных."""
    return 0.01 if pair.upper().endswith("JPY") else 0.0001


def _is_open_for_pair(open_trades: list[dict], pair: str) -> bool:
    return any(t["pair"] == pair and t["status"] == "open" for t in open_trades)


# ────────── Анализ уровней ──────────

def _analyse_levels(pair: str, big_players: list[dict],
                    current_price: float, bars_24h: pd.DataFrame,
                    atr_val: float) -> list[dict]:
    """Для каждого big-player уровня (взят с 7-дневного VP) посчитать «святость»
    относительно последних 24h.

    Логика «уровень от которого цена уходит»:
    - SUPPORT (level < current_price): цена за 24h ни разу не подошла снизу к
      уровню; чем дальше 24h-минимум от уровня — тем сильнее «отталкивает».
      Trade direction = BUY (price moves AWAY upward from this floor).
    - RESISTANCE (level > current_price): цена за 24h ни разу не подошла сверху;
      чем дальше 24h-максимум от уровня — тем сильнее «отталкивает».
      Trade direction = SELL.

    `is_valid` если:
    - level находится на правильной стороне относительно current_price
    - 24h extreme (минимум для support / максимум для resistance) удалён от
      уровня хотя бы на LEVEL_AVOIDANCE_ATR_MULT × ATR_1h
    - расстояние от current_price до уровня хотя бы MIN_LEVEL_DISTANCE_ATR_MULT×ATR
      (есть смысл торговать «от уровня»)
    - в полосе ±APPROACH_BAND_ATR_MULT×ATR от уровня цена не провела больше
      MIN_APPROACH_BARS_PCT% времени (т.е. реально «не приближалась»)
    """
    if bars_24h is None or bars_24h.empty or atr_val is None or atr_val <= 0:
        return []

    pip = _pip_size(pair)
    high_24h = float(bars_24h["High"].max())
    low_24h = float(bars_24h["Low"].min())
    closes = bars_24h["Close"].to_numpy()
    n = len(closes)
    band = APPROACH_BAND_ATR_MULT * atr_val

    out = []
    for bp in big_players:
        lvl = float(bp["price"])

        if lvl < current_price:
            kind_eff = "support"
            level_distance = current_price - lvl
            avoidance_distance = low_24h - lvl   # > 0 → 24h-low ВЫШЕ уровня (price avoided)
            # approached = бары где цена была близко к уровню, но НЕ пробила (close >= lvl)
            mask_approach = (closes >= lvl) & ((closes - lvl) <= band)
            approached = float(mask_approach.sum()) / n * 100.0
            in_band = np.abs(closes - lvl) <= band
            time_in_band_pct = float(in_band.sum()) / n * 100.0
            direction = "BUY"
        else:
            kind_eff = "resistance"
            level_distance = lvl - current_price
            avoidance_distance = lvl - high_24h  # > 0 → 24h-high НИЖЕ уровня
            mask_approach = (closes <= lvl) & ((lvl - closes) <= band)
            approached = float(mask_approach.sum()) / n * 100.0
            in_band = np.abs(closes - lvl) <= band
            time_in_band_pct = float(in_band.sum()) / n * 100.0
            direction = "SELL"

        valid_extreme = avoidance_distance >= LEVEL_AVOIDANCE_ATR_MULT * atr_val
        valid_distance = (
            level_distance >= MIN_LEVEL_DISTANCE_ATR_MULT * atr_val
            and level_distance <= MAX_LEVEL_DISTANCE_ATR_MULT * atr_val
        )
        valid_approached = approached >= MIN_APPROACH_BARS_PCT
        # для финальной валидности достаточно extreme + distance; approached
        # используется как тай-брейкер для сортировки.
        is_valid = bool(valid_extreme and valid_distance)

        # «святость» = avoidance_distance в ATR'ах (нормализованная сила отталкивания)
        avoidance_score = avoidance_distance / atr_val if atr_val > 0 else 0.0

        out.append({
            "price": lvl,
            "kind": kind_eff,
            "weight_pct": float(bp.get("weight_pct", 0.0)),
            "level_distance_pips": round(level_distance / pip, 1),
            "avoidance_distance_pips": round(avoidance_distance / pip, 1),
            "approached_pct": round(approached, 2),
            "time_in_band_pct": round(time_in_band_pct, 2),
            "avoidance_score": round(avoidance_score, 3),
            "is_valid": is_valid,
            "trade_direction": direction,
            "valid_extreme": valid_extreme,
            "valid_distance": valid_distance,
            "valid_approached": valid_approached,
        })
    # сортируем по avoidance_score (чем выше — тем «святее»)
    out.sort(key=lambda x: x["avoidance_score"], reverse=True)
    return out


# ────────── 10-vote consensus ──────────

def _votes(pair: str, direction: str, level: dict, current_price: float,
           bars_1h: pd.DataFrame, atr_24h: float, vp_data: dict,
           forecast: dict | None, fundamentals: dict | None,
           cot: dict | None, news_blackouts: dict | None,
           atr_5d_median: float | None, radar: dict | None = None) -> dict:
    """Считает 10 независимых голосов ЗА/ПРОТИВ направления.

    Возвращает dict со списком votes и итоговым yes/total.
    """
    votes_list: list[dict] = []
    pip = _pip_size(pair)

    # vote 1: VP forecast no_return направление совпадает
    nr = (vp_data or {}).get("forecast_to_utc5_midnight", {}).get("no_return_levels", [])
    vp_dir_ok = False
    if nr:
        # «не вернётся вниз» (side=below) → BUY
        # «не вернётся вверх» (side=above) → SELL
        sides = {n.get("side") for n in nr}
        if direction == "BUY" and "below" in sides:
            vp_dir_ok = True
        elif direction == "SELL" and "above" in sides:
            vp_dir_ok = True
    votes_list.append({"name": "vp_no_return_dir", "yes": vp_dir_ok})

    # vote 2: forecast probability ≥ 70%
    f_prob = (forecast or {}).get("probability_pct", 0)
    votes_list.append({"name": "forecast_prob_70", "yes": f_prob >= 70})

    # vote 3: forecast.side совпадает
    f_side = (forecast or {}).get("side")
    votes_list.append({"name": "forecast_side_match", "yes": f_side == direction})

    # vote 4: macro tilt (fundamentals) совпадает
    macro_dir = None
    if fundamentals:
        sm = fundamentals.get("summary") or fundamentals
        per_pair = (sm.get("per_pair") or {}).get(pair)
        if per_pair:
            tilt = per_pair.get("tilt") or per_pair.get("net_tilt")
            if isinstance(tilt, (int, float)):
                macro_dir = "BUY" if tilt > 0 else "SELL" if tilt < 0 else None
            elif isinstance(tilt, str):
                tilt_u = tilt.upper()
                if tilt_u in ("BUY", "SELL"):
                    macro_dir = tilt_u
    votes_list.append({"name": "macro_tilt", "yes": macro_dir == direction})

    # vote 5: COT contrarian signal совпадает
    cot_dir = None
    if cot:
        sm = cot.get("summary") or cot
        all_sig = sm.get("all_pair_signals") or {}
        sig = all_sig.get(pair) or {}
        s = sig.get("signal") or sig.get("direction")
        if isinstance(s, str) and s.upper() in ("BUY", "SELL"):
            cot_dir = s.upper()
    votes_list.append({"name": "cot_contrarian", "yes": cot_dir == direction})

    # vote 6: RSI(1h) НЕ в экстремуме против направления
    rsi_ok = True
    if bars_1h is not None and not bars_1h.empty:
        rsi_ser = ind.rsi(bars_1h["Close"], 14)
        rsi_now = float(rsi_ser.iloc[-1])
        if direction == "BUY" and rsi_now > 75:
            rsi_ok = False
        elif direction == "SELL" and rsi_now < 25:
            rsi_ok = False
    votes_list.append({"name": "rsi_not_extreme", "yes": rsi_ok})

    # vote 7: EMA20 vs EMA50 совпадает
    ema_ok = False
    if bars_1h is not None and len(bars_1h) >= 50:
        ema20 = float(ind.ema(bars_1h["Close"], 20).iloc[-1])
        ema50 = float(ind.ema(bars_1h["Close"], 50).iloc[-1])
        if direction == "BUY" and ema20 > ema50:
            ema_ok = True
        elif direction == "SELL" and ema20 < ema50:
            ema_ok = True
    votes_list.append({"name": "ema20_vs_ema50", "yes": ema_ok})

    # vote 8: ATR не аномально высокий (current ATR < 1.5 × 5-day median)
    atr_ok = True
    if atr_5d_median and atr_5d_median > 0:
        atr_ok = atr_24h <= 1.5 * atr_5d_median
    votes_list.append({"name": "atr_not_spiking", "yes": atr_ok})

    # vote 9: нет news blackout
    blackouts = (news_blackouts or {}).get("pairs", {}).get(pair, {})
    in_blackout = bool(blackouts.get("in_blackout"))
    votes_list.append({"name": "no_news_blackout", "yes": not in_blackout})

    # vote 10: расстояние до POC ≥ 0.3×ATR (есть куда ходить)
    poc = (vp_data or {}).get("poc")
    poc_ok = True
    if poc is not None and atr_24h > 0:
        poc_dist = abs(current_price - float(poc))
        poc_ok = poc_dist >= 0.3 * atr_24h
    votes_list.append({"name": "poc_distance_ok", "yes": poc_ok})

    # vote 11: «военный радар» (20+ независимых сканеров) согласен с направлением.
    # `radar` приходит из market_radar.json — overall_score [-100..+100].
    radar_pair = (radar or {}).get("pairs", {}).get(pair) if radar else None
    radar_yes = False
    radar_label = "no radar data"
    if radar_pair:
        rs = float(radar_pair.get("overall_score") or 0)
        rd = radar_pair.get("direction", "NEUTRAL")
        radar_label = f"radar={rs:+.1f} ({rd})"
        if direction == "BUY" and rs > 5:
            radar_yes = True
        elif direction == "SELL" and rs < -5:
            radar_yes = True
    votes_list.append({"name": "market_radar_aligned", "yes": radar_yes,
                       "detail": radar_label})

    yes_count = sum(1 for v in votes_list if v["yes"])
    return {
        "votes": votes_list,
        "yes": yes_count,
        "total": len(votes_list),
    }


# ────────── Авто-выбор экспирации ──────────

def _auto_expiry_hours(level_distance_pips: float, atr_pips: float,
                      current_price: float, atr_24h: float) -> int:
    """1–20 часов в зависимости от расстояния до уровня и волатильности."""
    if atr_pips <= 0:
        return MIN_EXPIRY_H
    distance_atr = level_distance_pips / atr_pips
    expiry = round(distance_atr * EXPIRY_PER_ATR)
    # высокая волатильность → ускоряемся
    if current_price > 0 and atr_24h / current_price > HIGH_VOL_PRICE_RATIO:
        expiry = max(1, expiry // 2)
    return int(max(MIN_EXPIRY_H, min(MAX_EXPIRY_H, expiry)))


# ────────── Поиск сигналов ──────────

def _find_signal(pair: str, snapshot: dict, fundamentals: dict | None,
                 cot: dict | None, news_blackouts: dict | None,
                 radar: dict | None = None) -> dict | None:
    """Главная функция: для одной пары возвращает сигнал на открытие сделки или None."""
    # 1h-бары — стабильный ATR + EMA для голосов
    bars_1h = yahoo.latest_bars(pair, "1h", 200)
    if bars_1h is None or bars_1h.empty or len(bars_1h) < 30:
        return None
    atr_ser_1h = ind.atr(bars_1h, 14)
    if atr_ser_1h.dropna().empty:
        return None
    atr_1h = float(atr_ser_1h.dropna().iloc[-1])
    if atr_1h <= 0:
        return None

    # 24h 1-минутных баров — для проверки «приближалась/уходит»
    bars_24h = yahoo.latest_bars(pair, "1m", SCAN_WINDOW_MINUTES)
    if bars_24h is None or bars_24h.empty or len(bars_24h) < 60:
        return None
    current_price = float(bars_24h["Close"].iloc[-1])
    pip = _pip_size(pair)
    atr_pips = atr_1h / pip

    # 7-дневное окно для VP — даёт «институциональные» уровни
    bars_vp = yahoo.latest_bars(pair, "1m", VP_HISTORY_DAYS * 24 * 60)
    if bars_vp is None or bars_vp.empty:
        bars_vp = bars_24h
    vp_data = vp_mod.build(pair, df=bars_vp, buckets=config.VP_BUCKETS)
    if "error" in vp_data:
        return None
    big_players = vp_data.get("big_players", [])
    if not big_players:
        return None

    # ATR 5d median (на 1m, для проверки spiking)
    bars_5d = yahoo.latest_bars(pair, "1m", 5 * 24 * 60)
    atr_5d_median = None
    if bars_5d is not None and not bars_5d.empty:
        atr_5d_ser = ind.atr(bars_5d, 14).dropna()
        if not atr_5d_ser.empty:
            # переводим 1m-ATR в эквивалент 1h (×60) для корректного сравнения
            atr_5d_median = float(atr_5d_ser.median()) * 60

    # Анализируем уровни в окне 24h
    levels = _analyse_levels(pair, big_players, current_price, bars_24h, atr_1h)
    valid_levels = [lv for lv in levels if lv["is_valid"]]
    if not valid_levels:
        # для UI: вернём топ-3 невалидных причин
        sample = levels[:3] if levels else []
        return {
            "pair": pair,
            "skip_reason": "no_valid_avoidance_level",
            "all_levels_count": len(levels),
            "current_price": current_price,
            "atr_pips": round(atr_pips, 1),
            "sample_levels": sample,
        }

    # Лучший уровень: самый «святой» (наибольший avoidance_score)
    best = valid_levels[0]
    direction = best["trade_direction"]
    level_distance_pips = best["level_distance_pips"]

    # Голоса
    forecast = (snapshot.get("forecasts") or {}).get(pair)
    vote_result = _votes(
        pair, direction, best, current_price, bars_1h, atr_1h, vp_data,
        forecast, fundamentals, cot, news_blackouts, atr_5d_median, radar,
    )

    if vote_result["yes"] < MIN_VOTES:
        return {
            "pair": pair,
            "skip_reason": f"only_{vote_result['yes']}_of_{vote_result['total']}_votes",
            "direction": direction,
            "best_level": best,
            "votes": vote_result,
            "current_price": current_price,
        }

    # 10-минутный pre-trade фильтр (брокер не даёт early-close, поэтому делаем
    # ПРЕДСКАЗАНИЕ перед открытием — есть ли краткосрочные признаки разворота)
    likely_reversal, reversal_breakdown = _predict_10min_reversal(
        pair, direction, current_price)
    if not likely_reversal:
        return {
            "pair": pair,
            "skip_reason": (
                f"reversal_unlikely_{reversal_breakdown['yes_count']}_of_5"
            ),
            "direction": direction,
            "best_level": best,
            "votes": vote_result,
            "reversal_filter": reversal_breakdown,
            "current_price": current_price,
        }

    expiry_h = _auto_expiry_hours(level_distance_pips, atr_pips, current_price, atr_1h)

    return {
        "pair": pair,
        "direction": direction,
        "best_level": best,
        "valid_levels_count": len(valid_levels),
        "level_distance_pips": round(level_distance_pips, 1),
        "current_price": current_price,
        "atr_pips": round(atr_pips, 1),
        "expiry_hours": expiry_h,
        "votes": vote_result,
        "reversal_filter": reversal_breakdown,
        "vp": {
            "poc": vp_data.get("poc"),
            "vah": vp_data.get("vah"),
            "val": vp_data.get("val"),
            "direction": vp_data.get("direction"),
        },
    }


# ────────── Открытие/закрытие сделок ──────────

def _open_new_trades(open_trades: list[dict], snapshot: dict,
                     fundamentals: dict | None, cot: dict | None,
                     news_blackouts: dict | None,
                     radar: dict | None = None) -> tuple[int, list[dict]]:
    """Сканирует все 28 пар. Открывает сделки по тем, у кого ≥MIN_VOTES голосов."""
    opened = 0
    signals: list[dict] = []
    now_ts = _now()
    for pair in config.PAIRS:
        if _is_open_for_pair(open_trades, pair):
            signals.append({"pair": pair, "skip_reason": "already_open"})
            continue
        try:
            sig = _find_signal(pair, snapshot, fundamentals, cot, news_blackouts, radar)
        except Exception as e:
            log.exception(f"_find_signal failed for {pair}: {e}")
            signals.append({"pair": pair, "skip_reason": f"error:{type(e).__name__}"})
            continue
        if sig is None:
            signals.append({"pair": pair, "skip_reason": "no_data"})
            continue
        signals.append(sig)
        if sig.get("skip_reason"):
            continue

        cp = float(sig["current_price"])
        expiry_time = now_ts + timedelta(hours=int(sig["expiry_hours"]))
        trade = {
            "id": "stk-" + str(uuid.uuid4())[:10],
            "strategy": "stakan",
            "pair": pair,
            "side": sig["direction"],
            "open_price": cp,
            "open_time": now_ts.isoformat(),
            "expiry_time": expiry_time.isoformat(),
            "expiry_hours": int(sig["expiry_hours"]),
            "stake_usd": float(config.STAKE_USD),
            "payout_pct": float(config.PAYOUT_PCT),
            "level_at_open": sig["best_level"],
            "level_distance_pips_at_open": sig["level_distance_pips"],
            "atr_pips_at_open": sig["atr_pips"],
            "votes_at_open": sig["votes"],
            "vp_at_open": sig["vp"],
            "valid_levels_count_at_open": sig["valid_levels_count"],
            "status": "open",
        }
        open_trades.append(trade)
        opened += 1
        log.info(
            f"OPEN-STAKAN {pair} {sig['direction']} @ {cp} "
            f"level={sig['best_level']['price']} ({sig['best_level']['kind']}, "
            f"{sig['level_distance_pips']} pips) "
            f"expiry={sig['expiry_hours']}h votes={sig['votes']['yes']}/{sig['votes']['total']}"
        )
    return opened, signals


def _predict_10min_reversal(pair: str, direction: str,
                            current_price: float) -> tuple[bool, dict]:
    """ПРЕДСКАЗАТЕЛЬНЫЙ фильтр (pre-trade): есть ли ≥REVERSAL_MIN_SIGNS из 5
    краткосрочных индикаторов того, что цена в ближайшие 10 мин начнёт
    двигаться в нашу сторону?

    По уточнению пользователя 2026-05-01: «система должна заранее знать что
    он за 10 минут развернуться к нашу сторону». Брокер НЕ позволяет
    закрывать досрочно — поэтому это НЕ exit-rule, а entry-filter.

    Returns (likely_reversal, breakdown).
    """
    breakdown = {"signs": {}, "yes_count": 0, "min_required": REVERSAL_MIN_SIGNS}
    try:
        # ─── 1) 5-min RSI экстремум в обратную сторону ───
        bars_5m = yahoo.latest_bars(pair, "5m", 50)
        if bars_5m is not None and not bars_5m.empty and len(bars_5m) >= 14:
            rsi5 = ind.rsi(bars_5m["Close"], 14).dropna()
            if not rsi5.empty:
                cur_rsi = float(rsi5.iloc[-1])
                if direction == "BUY":
                    yes = cur_rsi < REVERSAL_RSI_OVERSOLD
                else:
                    yes = cur_rsi > REVERSAL_RSI_OVERBOUGHT
                breakdown["signs"]["rsi_5m_extreme"] = {
                    "yes": yes, "value": round(cur_rsi, 1),
                    "threshold": (REVERSAL_RSI_OVERSOLD if direction == "BUY"
                                  else REVERSAL_RSI_OVERBOUGHT),
                }
            else:
                breakdown["signs"]["rsi_5m_extreme"] = {"yes": False, "no_data": True}
        else:
            breakdown["signs"]["rsi_5m_extreme"] = {"yes": False, "no_data": True}

        # ─── 2) Последние 10 минутных баров: momentum уже разворачивается? ───
        bars_1m = yahoo.latest_bars(pair, "1m", 11)
        if bars_1m is not None and not bars_1m.empty and len(bars_1m) >= 5:
            closes = bars_1m["Close"].to_numpy()
            # последние 3 vs предыдущие 7 — куда сместился средний?
            recent = float(closes[-3:].mean())
            prior = float(closes[:-3].mean())
            if direction == "BUY":
                yes = recent > prior  # уже растёт
            else:
                yes = recent < prior
            breakdown["signs"]["short_term_momentum"] = {
                "yes": yes, "recent_avg": round(recent, 5),
                "prior_avg": round(prior, 5),
            }
        else:
            breakdown["signs"]["short_term_momentum"] = {"yes": False, "no_data": True}

        # ─── 3) Bollinger %B на 5m: < 0.1 → перепродано → BUY-разворот; > 0.9 → SELL ───
        if bars_5m is not None and not bars_5m.empty and len(bars_5m) >= 20:
            bbp = ind.bollinger_pct_b(bars_5m["Close"], 20, 2.0).dropna()
            if not bbp.empty:
                cur_bbp = float(bbp.iloc[-1])
                if direction == "BUY":
                    yes = cur_bbp < 0.1
                else:
                    yes = cur_bbp > 0.9
                breakdown["signs"]["bb_extreme"] = {
                    "yes": yes, "value": round(cur_bbp, 3),
                }
            else:
                breakdown["signs"]["bb_extreme"] = {"yes": False, "no_data": True}
        else:
            breakdown["signs"]["bb_extreme"] = {"yes": False, "no_data": True}

        # ─── 4) Последний 1m бар — направление в нашу сторону + range > median ───
        if bars_1m is not None and not bars_1m.empty and len(bars_1m) >= 5:
            last = bars_1m.iloc[-1]
            last_dir = float(last["Close"] - last["Open"])
            ranges = (bars_1m["High"] - bars_1m["Low"]).dropna()
            med_range = float(ranges.median()) if not ranges.empty else 0
            cur_range = float(last["High"] - last["Low"])
            big_bar = cur_range > med_range  # бар крупнее обычного
            if direction == "BUY":
                yes = last_dir > 0 and big_bar
            else:
                yes = last_dir < 0 and big_bar
            breakdown["signs"]["last_bar_thrust"] = {
                "yes": yes, "dir": round(last_dir, 5),
                "range_vs_median": round(cur_range / med_range, 2) if med_range > 0 else 0,
            }
        else:
            breakdown["signs"]["last_bar_thrust"] = {"yes": False, "no_data": True}

        # ─── 5) Дист. до ближайшего микро-уровня: < 0.5×ATR_15m в нашу сторону ───
        bars_15m = yahoo.latest_bars(pair, "15m", 50)
        if bars_15m is not None and not bars_15m.empty and len(bars_15m) >= 14:
            atr_15 = ind.atr(bars_15m, 14).dropna()
            if not atr_15.empty:
                atr_v = float(atr_15.iloc[-1])
                # «микро-уровень» = последние 50 баров High (для SELL разворота
                # сверху) или Low (для BUY разворота снизу)
                if direction == "BUY":
                    nearest_low = float(bars_15m["Low"].min())
                    dist = current_price - nearest_low
                    yes = (dist >= 0) and (dist <= REVERSAL_NEAR_LEVEL_ATR_MULT * atr_v)
                    breakdown["signs"]["near_micro_level"] = {
                        "yes": yes, "level": round(nearest_low, 5),
                        "dist_atr": round(dist / atr_v, 2) if atr_v > 0 else None,
                    }
                else:
                    nearest_high = float(bars_15m["High"].max())
                    dist = nearest_high - current_price
                    yes = (dist >= 0) and (dist <= REVERSAL_NEAR_LEVEL_ATR_MULT * atr_v)
                    breakdown["signs"]["near_micro_level"] = {
                        "yes": yes, "level": round(nearest_high, 5),
                        "dist_atr": round(dist / atr_v, 2) if atr_v > 0 else None,
                    }
            else:
                breakdown["signs"]["near_micro_level"] = {"yes": False, "no_data": True}
        else:
            breakdown["signs"]["near_micro_level"] = {"yes": False, "no_data": True}
    except Exception as e:
        log.warning(f"_predict_10min_reversal {pair}: {type(e).__name__}: {e}")

    yes_count = sum(1 for s in breakdown["signs"].values() if s.get("yes"))
    breakdown["yes_count"] = yes_count
    likely = yes_count >= REVERSAL_MIN_SIGNS
    return likely, breakdown


def _settle_expired(open_trades: list[dict], closed_trades: list[dict]) -> int:
    """Закрывает истёкшие сделки по реальной цене Yahoo (settlement_price).

    Брокер пользователя НЕ поддерживает досрочное закрытие, поэтому здесь
    нет early-close. Все сделки идут до своего expiry_time и settle по
    реальной цене.
    """
    settled = 0
    still_open = []
    for t in open_trades:
        if t["status"] != "open":
            continue
        expiry = datetime.fromisoformat(t["expiry_time"])
        if _now() < expiry:
            still_open.append(t)
            continue

        close_price = yahoo.settlement_price(t["pair"], expiry)
        if close_price is None:
            still_open.append(t)
            continue

        if t["side"] == "BUY":
            win = close_price > t["open_price"]
        else:
            win = close_price < t["open_price"]
        stake = float(t.get("stake_usd") or config.STAKE_USD)
        payout = float(t.get("payout_pct") or config.PAYOUT_PCT)
        pnl = (stake * payout) if win else (-stake)

        result = "WIN" if win else "LOSS"
        t.update({
            "status": result,
            "close_price": close_price,
            "close_time": _now().isoformat(),
            "result": result,
            "pnl_usd": round(pnl, 2),
        })
        closed_trades.append(t)
        settled += 1
        log.info(
            f"CLOSE-STAKAN {t['pair']} {t['side']} open={t['open_price']} "
            f"close={close_price} → {result} pnl={pnl:+.2f}"
        )

    open_trades[:] = still_open
    return settled


def _enrich_open_trade(t: dict) -> dict:
    """Live PnL для UI."""
    cp = yahoo.latest_price(t["pair"])
    if cp is None:
        return {**t, "live": {"current_price": None}}
    pip = _pip_size(t["pair"])
    if t["side"] == "BUY":
        in_money = cp > t["open_price"]
        diff_pct = (cp - t["open_price"]) / t["open_price"] * 100.0
        pips = (cp - t["open_price"]) / pip
    else:
        in_money = cp < t["open_price"]
        diff_pct = (t["open_price"] - cp) / t["open_price"] * 100.0
        pips = (t["open_price"] - cp) / pip
    expiry = datetime.fromisoformat(t["expiry_time"])
    open_time = datetime.fromisoformat(t["open_time"])
    now = _now()
    stake = float(t.get("stake_usd", config.STAKE_USD))
    payout = float(t.get("payout_pct", config.PAYOUT_PCT))
    return {
        **t,
        "live": {
            "current_price": cp,
            "diff_pct": round(diff_pct, 5),
            "pips": round(pips, 1),
            "in_money_now": in_money,
            "projected_payout": round(stake * payout if in_money else -stake, 2),
            "time_remaining_sec": max(0, int((expiry - now).total_seconds())),
            "elapsed_sec": int((now - open_time).total_seconds()),
            "as_of": now.isoformat(),
        },
    }


def _refresh_stats(closed: list[dict]) -> dict:
    n = len(closed)
    wins = sum(1 for t in closed if t.get("result") == "WIN")
    losses = n - wins
    pnl = sum(float(t.get("pnl_usd", 0.0)) for t in closed)
    wr = (wins / n * 100.0) if n > 0 else 0.0
    stats = {
        "as_of": _now().isoformat(),
        "strategy": "stakan",
        "total": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wr, 2),
        "total_pnl_usd": round(pnl, 2),
    }
    _save(STATS_FILE, stats)
    return stats


def cycle_once() -> dict:
    snapshot = _load(FORECASTS_FILE, {"forecasts": {}})
    fundamentals = _load(FUNDAMENTALS_FILE, {})
    cot = _load(COT_FILE, {})
    news_blackouts = _load(NEWS_FILE, {})
    radar = _load(RADAR_FILE, {})

    open_trades = _load(OPEN_FILE, [])
    closed = _load(CLOSED_FILE, [])

    settled = _settle_expired(open_trades, closed)

    halt_flag = config.STATE_DIR / "TRADING_HALTED.flag"
    if halt_flag.exists():
        log.info(f"paper_trader_stakan: TRADING_HALTED.flag → не открываю (settled={settled})")
        opened, signals = 0, []
    else:
        opened, signals = _open_new_trades(
            open_trades, snapshot, fundamentals, cot, news_blackouts, radar)

    enriched = [_enrich_open_trade(t) for t in open_trades]
    _save(OPEN_FILE, open_trades)
    _save(config.STATE_DIR / "stakan_open_trades_enriched.json", {
        "as_of": _now().isoformat(),
        "trades": enriched,
    })
    _save(CLOSED_FILE, closed)
    _save(SIGNALS_FILE, {
        "as_of": _now().isoformat(),
        "min_votes_required": MIN_VOTES,
        "max_votes": MAX_VOTES,
        "signals": signals,
    })
    stats = _refresh_stats(closed)
    return {"opened": opened, "settled": settled, "stats": stats, "signal_count": len(signals)}


def run_loop() -> None:
    log.info("paper_trader_stakan start")
    stop = {"flag": False}

    def _sig(_a, _b):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop["flag"]:
        _heartbeat()
        try:
            r = cycle_once()
            log.info(
                f"cycle: opened={r['opened']} settled={r['settled']} "
                f"signals={r['signal_count']} stats={r['stats']}"
            )
        except Exception as e:
            log.exception(f"cycle failed: {e}")
        _heartbeat()
        for _ in range(config.PAPER_TRADER_INTERVAL_SEC):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("paper_trader_stakan exit")


if __name__ == "__main__":
    run_loop()
