"""СТАКАН view — unified Order Book / Market Depth payload for the dashboard.

Объединяет в один JSON всё, что нужно для нового раздела «СТАКАН» на главной
странице:

- volume_profile (POC/VAH/VAL/buckets/big_players/no_return_levels)
- forecast (side / probability_pct / score / agents_for / agents_against)
- buyers_vs_sellers — баланс крупных игроков выше/ниже текущей цены
- bias_24h — направление на 24 часа (UP/DOWN) с уверенностью 0–100
- main_forecast_5h — основной прогноз на 1–5 часов с целью + no-return уровнем
- per_session_strategy — выбранный variant для текущей сессии этой пары
- as_of, market_open

Эндпоинты:
- GET /api/stakan-view/{pair}  — полный snapshot для одной пары
- GET /api/stakan-view         — компактный snapshot по всем 28 парам

User context (verbatim):
> "я хочу чтобы ты добавил основной особой раздел где будет стакан для каждой
> валюты будет возможность выбрать валюту и будет показан там визуально
> ордеров там будет показано что там происходит какие игроки есть и важно
> чтобы система показала куда будет стремиться рынок … за 24 часа будет
> рост или падение и этот прогноз должен дать результат 5 часов … система
> может давать мне прогноз от одного до пяти часов экспрессы и это будет
> основной прогноз для меня"

Источник данных — ТОЛЬКО уже существующие state/*.json (forecasts.json,
strategy_config.json) + on-demand vp_mod.build(pair). Никаких новых I/O в
горячем пути; данные обновляются forecast_scanner-ом раз в 5 мин, а на фронте
этот endpoint опрашивается каждые 10 сек (фронт сам решит когда показать
«stale»).
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from . import volume_profile as vp_mod


_FORECASTS_FILE = config.STATE_DIR / "forecasts.json"
_STRATEGY_FILE = config.STATE_DIR / "strategy_config.json"
_RADAR_FILE = config.STATE_DIR / "market_radar.json"
_FUNDAMENTALS_FILE = config.STATE_DIR / "agent_analyzer_fundamental_macro.json"
_COT_FILE = config.STATE_DIR / "agent_analyzer_cot_positioning.json"


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _safe_get(d: dict, *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _current_session_label(now_utc: datetime | None = None) -> str:
    """Совпадает с strategy_search.SESSION_WINDOWS / forecast_scanner._current_session.

    Возвращает 'Asia' | 'London' | 'Overlap' | 'NY' | 'Off'.
    """
    h = (now_utc or datetime.now(timezone.utc)).hour
    # см. teamagent/strategy_search.py SESSION_WINDOWS
    windows = {
        "Asia": (0, 7),
        "London": (7, 13),
        "Overlap": (13, 17),
        "NY": (17, 22),
    }
    for name, (lo, hi) in windows.items():
        if lo <= h < hi:
            return name
    return "Off"


# ────────────────────────────────────────────────────────────────────────────
# 1. Buyers vs Sellers split
# ────────────────────────────────────────────────────────────────────────────
def _buyers_sellers_balance(vp: dict) -> dict:
    """Считаем баланс «покупателей vs продавцов» по volume profile.

    Логика: всё что НИЖЕ текущей цены — институциональная поддержка (buyers),
    всё что ВЫШЕ — сопротивление (sellers). Big players получают вес ×3,
    обычные бакеты — вес ×1. Возвращаем %.
    """
    if vp.get("error") or not vp.get("buckets"):
        return {"buyers_pct": 50.0, "sellers_pct": 50.0, "favorite": "neutral"}

    cur = float(vp.get("current_price") or 0)
    buyers_w = sellers_w = 0.0

    for b in vp.get("buckets", []):
        price = float(b.get("price") or 0)
        w = float(b.get("weight_pct") or 0)
        if price <= cur:
            buyers_w += w
        else:
            sellers_w += w

    # boost from big_players (≥80-perc по объёму)
    for bp in vp.get("big_players", []):
        price = float(bp.get("price") or 0)
        w = float(bp.get("weight_pct") or 0) * 2.0  # дополнительный вес
        if price <= cur:
            buyers_w += w
        else:
            sellers_w += w

    total = buyers_w + sellers_w
    if total <= 0:
        return {"buyers_pct": 50.0, "sellers_pct": 50.0, "favorite": "neutral"}

    bp = round(buyers_w / total * 100.0, 1)
    sp = round(100.0 - bp, 1)
    favorite = "buyers" if bp > sp + 5 else "sellers" if sp > bp + 5 else "neutral"
    return {
        "buyers_pct": bp,
        "sellers_pct": sp,
        "favorite": favorite,
        "buyers_weight": round(buyers_w, 2),
        "sellers_weight": round(sellers_w, 2),
    }


# ────────────────────────────────────────────────────────────────────────────
# 2. 24-hour direction bias (multi-source ensemble)
# ────────────────────────────────────────────────────────────────────────────
def _bias_24h(forecast: dict, vp: dict, radar_score: float | None,
              fundamentals: dict | None, cot: dict | None) -> dict:
    """Куда рынок хочет двигаться в горизонте 24 часов.

    Голосуют 8 источников; финальная уверенность = доля голосов + magnitude.
    """
    votes_up = 0.0
    votes_dn = 0.0
    why: list[str] = []

    side = forecast.get("side")
    prob = float(forecast.get("probability_pct") or 0)

    # 1. Forecast probability + side (вес 3)
    if side == "BUY" and prob >= 50:
        votes_up += 3 * (prob / 100)
        why.append(f"forecast {prob:.1f}% BUY")
    elif side == "SELL" and prob >= 50:
        votes_dn += 3 * (prob / 100)
        why.append(f"forecast {prob:.1f}% SELL")

    # 2. EMA stack 4H (вес 2)
    ind4 = forecast.get("indicators", {}).get("4H", {}) or {}
    e20 = ind4.get("ema20"); e50 = ind4.get("ema50"); e200 = ind4.get("ema200")
    if e20 and e50 and e200:
        if e20 > e50 > e200:
            votes_up += 2; why.append("4H EMA-stack BUY (20>50>200)")
        elif e20 < e50 < e200:
            votes_dn += 2; why.append("4H EMA-stack SELL (20<50<200)")

    # 3. ADX trend strength (вес 1.5)
    adx = ind4.get("adx") or 0
    plus_di = ind4.get("plus_di") or 0
    minus_di = ind4.get("minus_di") or 0
    if adx >= 25 and plus_di > minus_di + 3:
        votes_up += 1.5; why.append(f"4H ADX={adx:.0f} тренд BUY")
    elif adx >= 25 and minus_di > plus_di + 3:
        votes_dn += 1.5; why.append(f"4H ADX={adx:.0f} тренд SELL")

    # 4. Volume profile direction (вес 1)
    if vp.get("direction") == "UP":
        votes_up += 1; why.append("VP импульс UP")
    elif vp.get("direction") == "DOWN":
        votes_dn += 1; why.append("VP импульс DOWN")

    # 5. Market radar (вес до 2)
    if radar_score is not None and abs(radar_score) >= 10:
        weight = min(2.0, abs(radar_score) / 100 * 4)
        if radar_score > 0:
            votes_up += weight; why.append(f"market_radar {radar_score:+.0f}")
        else:
            votes_dn += weight; why.append(f"market_radar {radar_score:+.0f}")

    # 6. Fundamentals (FRED macro tilt, вес 1)
    if fundamentals:
        pair_data = fundamentals.get("pairs", {}).get(forecast.get("pair", "")) or {}
        tilt = pair_data.get("tilt_pct")
        if tilt is not None:
            if tilt > 1:
                votes_up += 1; why.append(f"FRED macro {tilt:+.1f}%")
            elif tilt < -1:
                votes_dn += 1; why.append(f"FRED macro {tilt:+.1f}%")

    # 7. COT contrarian (вес 1)
    if cot:
        sig = cot.get("pairs", {}).get(forecast.get("pair", "")) or {}
        if sig.get("side") == "BUY":
            votes_up += 1; why.append(f"COT BUY ({sig.get('strength_pct', 0):.0f}%)")
        elif sig.get("side") == "SELL":
            votes_dn += 1; why.append(f"COT SELL ({sig.get('strength_pct', 0):.0f}%)")

    # 8. MACD 1H (вес 1)
    ind1 = forecast.get("indicators", {}).get("1H", {}) or {}
    macd_h = ind1.get("macd_hist") or 0
    if macd_h > 0:
        votes_up += 1; why.append("1H MACD>0")
    elif macd_h < 0:
        votes_dn += 1; why.append("1H MACD<0")

    total = votes_up + votes_dn
    if total <= 0:
        return {
            "direction": "FLAT",
            "confidence_pct": 50.0,
            "votes_up": 0.0,
            "votes_down": 0.0,
            "reasoning": ["нет согласия источников"],
        }

    if votes_up > votes_dn:
        direction = "UP"
        conf = 50 + (votes_up - votes_dn) / total * 50
    else:
        direction = "DOWN"
        conf = 50 + (votes_dn - votes_up) / total * 50

    return {
        "direction": direction,
        "confidence_pct": round(min(95.0, conf), 1),
        "votes_up": round(votes_up, 2),
        "votes_down": round(votes_dn, 2),
        "reasoning": why[:8],
    }


# ────────────────────────────────────────────────────────────────────────────
# 3. Main 1–5h forecast (the user's "ОСНОВНОЙ ПРОГНОЗ для меня")
# ────────────────────────────────────────────────────────────────────────────
def _main_forecast_5h(forecast: dict, vp: dict, bias: dict) -> dict:
    """Основной прогноз 1–5 часов: side, entry, target, no-return, hours.

    Логика:
    - side берём из forecast
    - hours = forecast.recommended_hours (1..5)
    - entry = current_price
    - no_return_level = ближайший big_player на противоположной стороне (от
      него цена не должна возвращаться)
    - target_price = entry + side*ATR_1H*(1 + probability)
    - probability_pct = forecast.probability_pct, скорректировано вкладом
      bias_24h confidence
    """
    side = forecast.get("side") or "—"
    entry = float(forecast.get("current_price") or 0)
    hours = int(forecast.get("recommended_hours") or 2)
    hours = max(1, min(5, hours))

    ind = forecast.get("indicators", {}).get("1H", {}) or {}
    atr = float(ind.get("atr14") or 0)

    # JPY pairs ≈ 100 pip multiplier, остальные 10000
    pair = forecast.get("pair") or ""
    pip_mul = 100 if pair.endswith("JPY") else 10000

    sign = 1 if side == "BUY" else -1 if side == "SELL" else 0
    prob = float(forecast.get("probability_pct") or 0)

    # Бонус за согласие 24h-bias с этой стороной
    bias_dir = bias.get("direction")
    bias_conf = float(bias.get("confidence_pct") or 50)
    aligned = (sign > 0 and bias_dir == "UP") or (sign < 0 and bias_dir == "DOWN")
    prob_adj = prob + (5 if aligned else -5 if bias_dir in ("UP", "DOWN") else 0)
    prob_adj = max(50.0, min(95.0, prob_adj))

    # target = entry + sign × ATR × (1 + prob/100) × hours/3 (длиннее → дальше)
    if atr > 0 and sign != 0:
        target = entry + sign * atr * (1.0 + prob / 100.0) * (hours / 3.0)
        target_pips = abs(target - entry) * pip_mul
    else:
        target = None
        target_pips = None

    # no-return: ближайший big_player на ПРОТИВОПОЛОЖНОЙ стороне
    no_return_price = None
    no_return_pips = None
    big_players = vp.get("big_players", []) or []
    if sign != 0 and entry > 0:
        candidates = [
            float(bp.get("price") or 0) for bp in big_players
            if (sign > 0 and float(bp.get("price") or 0) < entry)
            or (sign < 0 and float(bp.get("price") or 0) > entry)
        ]
        if candidates:
            # ближайший в противоположную сторону = ближайшая «опора»/«потолок»
            no_return_price = min(candidates, key=lambda p: abs(p - entry))
            no_return_pips = abs(no_return_price - entry) * pip_mul

    # safety stop = entry - sign × ATR × 1.0 (грубая страховка)
    safety_stop = entry - sign * atr * 1.0 if (atr and sign) else None

    return {
        "side": side,
        "hours": hours,
        "entry_price": round(entry, 5),
        "target_price": round(target, 5) if target is not None else None,
        "target_pips": round(target_pips, 1) if target_pips is not None else None,
        "no_return_price": round(no_return_price, 5) if no_return_price is not None else None,
        "no_return_pips": round(no_return_pips, 1) if no_return_pips is not None else None,
        "safety_stop_price": round(safety_stop, 5) if safety_stop is not None else None,
        "probability_pct": round(prob_adj, 1),
        "atr_1h": atr,
        "pip_mul": pip_mul,
        "explain_ru": (
            f"Прогноз: {side} в горизонте {hours}ч. Вход {entry:.5f}, "
            f"цель ≈ {target_pips:.0f} pips, не-возвратный уровень ≈ "
            f"{no_return_pips:.0f} pips ниже/выше. "
            f"Если цена пробьёт not-return и закрепится — прогноз отменяется."
            if (target_pips is not None and no_return_pips is not None)
            else f"Прогноз: {side} в горизонте {hours}ч. Вход {entry:.5f}."
        ),
    }


# ────────────────────────────────────────────────────────────────────────────
# 4. Per-session strategy chosen for current pair
# ────────────────────────────────────────────────────────────────────────────
def _per_session_strategy(pair: str, strategy_cfg: dict, current_session: str) -> dict:
    pair_data = (strategy_cfg.get("pairs", {}) or {}).get(pair, {}) or {}
    by_session = pair_data.get("by_session", {}) or {}
    sess = by_session.get(current_session, {}) or {}

    # Список всех 4 сессий с их WR — для UI
    all_sessions = []
    for name in ("Asia", "London", "Overlap", "NY"):
        s = by_session.get(name, {}) or {}
        all_sessions.append({
            "session": name,
            "best_variant": s.get("best_variant"),
            "best_label": s.get("best_label"),
            "win_rate_pct": s.get("win_rate_pct"),
            "trades": s.get("trades"),
            "qualifies_70pct": bool(s.get("qualifies_70pct")),
        })

    return {
        "pair": pair,
        "current_session": current_session,
        "best_variant": sess.get("best_variant") or pair_data.get("best_variant"),
        "best_label": sess.get("best_label") or pair_data.get("best_label"),
        "win_rate_pct": sess.get("win_rate_pct"),
        "trades": sess.get("trades"),
        "qualifies_70pct": bool(sess.get("qualifies_70pct")),
        "globally_qualifies_70pct": bool(pair_data.get("qualifies_70pct")),
        "sessions_qualified_70pct": pair_data.get("sessions_qualified_70pct") or [],
        "all_sessions": all_sessions,
    }


# ────────────────────────────────────────────────────────────────────────────
# 5. Public API: full snapshot for one pair
# ────────────────────────────────────────────────────────────────────────────
def build_view(pair: str) -> dict:
    pair = pair.upper()
    if pair not in config.PAIRS:
        return {"error": "unknown_pair", "pair": pair}

    forecasts_blob = _load(_FORECASTS_FILE, {"forecasts": {}})
    forecast = (forecasts_blob.get("forecasts") or {}).get(pair) or {}
    strategy_cfg = _load(_STRATEGY_FILE, {"pairs": {}})
    radar = _load(_RADAR_FILE, {"pairs": {}})
    fundamentals = _load(_FUNDAMENTALS_FILE, {})
    cot = _load(_COT_FILE, {})

    # Свежий VP — он у forecast уже есть, но если файл пустой — попытаемся
    # построить on-demand. Стоимость одного построения VP — единицы мс
    # (yahoo cached), если кэш горячий.
    vp = forecast.get("volume_profile") or {}
    if not vp or vp.get("error"):
        try:
            vp = vp_mod.build(pair)
        except Exception as e:
            vp = {"error": f"vp_build_failed: {e}"}

    radar_score = _safe_get(radar, "pairs", pair, "overall_score")
    if radar_score is None:
        # legacy schema
        radar_score = _safe_get(radar, "pairs", pair, "score")

    bs = _buyers_sellers_balance(vp)
    bias = _bias_24h(forecast, vp, radar_score, fundamentals, cot)
    main5 = _main_forecast_5h(forecast, vp, bias)

    current_session = forecast.get("session") or _current_session_label()
    strat = _per_session_strategy(pair, strategy_cfg, current_session)

    return {
        "pair": pair,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "forecast_as_of": forecast.get("as_of"),
        "current_session": current_session,
        "current_price": forecast.get("current_price") or vp.get("current_price"),
        "forecast": {
            "side": forecast.get("side"),
            "probability_pct": forecast.get("probability_pct"),
            "score": forecast.get("score"),
            "max_score": forecast.get("max_score"),
            "recommended_hours": forecast.get("recommended_hours"),
            "agents_for": forecast.get("agents_for") or [],
            "agents_against": forecast.get("agents_against") or [],
            "agents_for_count": forecast.get("agents_for_count"),
            "agents_against_count": forecast.get("agents_against_count"),
        },
        "volume_profile": {
            "current_price": vp.get("current_price"),
            "high": vp.get("high"),
            "low": vp.get("low"),
            "poc": vp.get("poc"),
            "vah": vp.get("vah"),
            "val": vp.get("val"),
            "buckets": vp.get("buckets") or [],
            "big_players": vp.get("big_players") or [],
            "direction": vp.get("direction"),
            "no_return_levels": _safe_get(
                vp, "forecast_to_utc5_midnight", "no_return_levels", default=[]
            ),
        },
        "buyers_vs_sellers": bs,
        "bias_24h": bias,
        "main_forecast_5h": main5,
        "per_session_strategy": strat,
        "radar_score": radar_score,
    }


# ────────────────────────────────────────────────────────────────────────────
# 6. Public API: compact snapshot for all 28 pairs (for the picker grid)
# ────────────────────────────────────────────────────────────────────────────
def build_all_summary() -> dict:
    """Лёгкий вью по 28 парам — для селектора валюты сверху раздела."""
    forecasts_blob = _load(_FORECASTS_FILE, {"forecasts": {}})
    forecasts = forecasts_blob.get("forecasts") or {}
    strategy_cfg = _load(_STRATEGY_FILE, {"pairs": {}})
    items = []
    sess = _current_session_label()
    for p in config.PAIRS:
        f = forecasts.get(p, {}) or {}
        cfg = (strategy_cfg.get("pairs", {}) or {}).get(p, {}) or {}
        sess_data = (cfg.get("by_session", {}) or {}).get(sess, {}) or {}
        items.append({
            "pair": p,
            "side": f.get("side"),
            "probability_pct": f.get("probability_pct"),
            "current_price": f.get("current_price"),
            "recommended_hours": f.get("recommended_hours"),
            "session": f.get("session") or sess,
            "session_qualifies_70pct": bool(sess_data.get("qualifies_70pct")),
            "session_wr_pct": sess_data.get("win_rate_pct"),
        })
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "current_session": sess,
        "items": items,
    }
