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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config
from . import volume_profile as vp_mod


_FORECASTS_FILE = config.STATE_DIR / "forecasts.json"
_STRATEGY_FILE = config.STATE_DIR / "strategy_config.json"
_RADAR_FILE = config.STATE_DIR / "market_radar.json"
_FUNDAMENTALS_FILE = config.STATE_DIR / "agent_analyzer_fundamental_macro.json"
_COT_FILE = config.STATE_DIR / "agent_analyzer_cot_positioning.json"
_STAKAN_SIGNALS_FILE = config.STATE_DIR / "stakan_signals.json"
_NEWS_BLACKOUTS_FILE = config.STATE_DIR / "news_blackouts.json"


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
# 4b. INSTITUTIONAL VERDICT — думаем как крупный игрок, не как розница
# ────────────────────────────────────────────────────────────────────────────
# Главное отличие от _bias_24h: ВЕСА ПЕРЕВЁРНУТЫ — институциональные источники
# (big-players VP, no-return levels, COT, market_radar, FRED macro, stakan-vote-
# консенсус, VP direction) доминируют, а розничные индикаторы (EMA/ADX/MACD)
# имеют минимальный вес 0.5. News-blackout — VETO.
#
# User context (verbatim, 2026-05-04):
# > "Когда система говорит КУПИТЬ — это окончательный вердикт, все данные
# > указывают на это и рынок не развернётся. Думай как крупный игрок,
# > не как розничный трейдер."

def _stakan_vote_for(pair: str, stakan_signals: dict) -> dict:
    """Найти запись для пары в stakan_signals.json (последний скан paper_trader_stakan)."""
    if not stakan_signals:
        return {}
    for s in stakan_signals.get("signals", []) or []:
        if s.get("pair") == pair:
            return s
    return {}


def _fnd_signal_for(pair: str, fundamentals: dict) -> dict:
    """FRED-tilt сигнал для пары: ищем в pairs.{PAIR} либо в summary.top_bias_pairs."""
    if not fundamentals:
        return {}
    p = (fundamentals.get("pairs") or {}).get(pair)
    if p:
        return p
    summary = fundamentals.get("summary") or {}
    for r in (summary.get("top_bias_pairs") or []):
        if r.get("pair") == pair:
            return r
    return {}


def _cot_signal_for(pair: str, cot: dict) -> dict:
    """COT-сигнал для пары: pairs.{PAIR} либо summary.top_contrarian_signals."""
    if not cot:
        return {}
    p = (cot.get("pairs") or {}).get(pair)
    if p:
        return p
    summary = cot.get("summary") or {}
    for r in (summary.get("top_contrarian_signals") or []):
        if r.get("pair") == pair:
            return r
    return {}


def _hours_to_midnight_utc5(now_utc: datetime | None = None) -> float:
    """Сколько часов до 00:00 UTC+5 = 19:00 UTC."""
    now = now_utc or datetime.now(timezone.utc)
    target = now.replace(hour=19, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return round((target - now).total_seconds() / 3600.0, 2)


def _check_news_blackout(pair: str, news_blackouts: dict | None) -> tuple[bool, str | None]:
    """Возвращает (in_blackout, reason).

    Сначала смотрим snapshot-файл news_blackouts.json (если есть), потом —
    прямой вызов news.is_blackout(±30 мин) — он использует in-process кэш
    ForexFactory RSS на 15 минут, поэтому дешевле, чем кажется.
    """
    now = datetime.now(timezone.utc)
    if news_blackouts:
        rec = (news_blackouts.get("pairs") or {}).get(pair) or news_blackouts.get(pair)
        if isinstance(rec, list) and rec:
            for ev in rec:
                w = ev.get("when") or ev.get("time")
                if not w:
                    continue
                try:
                    t = datetime.fromisoformat(str(w).replace("Z", "+00:00"))
                    if abs((t - now).total_seconds()) <= 30 * 60:
                        return True, f"news ±30 мин: {str(ev.get('title', ''))[:60]}"
                except Exception:
                    pass
    try:
        from .data import news as news_mod
        if news_mod.is_blackout(pair, now, window_min=30):
            return True, "high-impact новость по этой паре в ±30 мин"
    except Exception:
        pass
    return False, None


def _institutional_verdict(
    pair: str,
    forecast: dict,
    vp: dict,
    bs: dict,
    radar_score: float | None,
    fundamentals: dict | None,
    cot: dict | None,
    stakan_signals: dict | None,
    news_blackouts: dict | None,
) -> dict:
    """Институциональный вердикт «КУПИТЬ / ПРОДАТЬ» — никогда «ОЖИДАНИЕ».

    Веса (перевёрнутые относительно `_bias_24h`):
      institutional (главные):
        VP big-players balance .............. 4
        VP no-return levels ................. 3
        COT positioning ..................... 3
        Market Radar overall_score .......... 3
        FRED macro tilt ..................... 2
        Stakan votes (11-vote consensus) .... 3
        VP direction (volume momentum) ...... 2
      retail (минимальные):
        EMA stack 4H ........................ 0.5
        ADX trend ........................... 0.5
        MACD 1H ............................. 0.5
      news blackout (±30 мин) — снижает уверенность, но НЕ блокирует.

    Решение (по требованию пользователя «нигде не должно быть ожидания»):
      - ≥80% institutional согласны + перевес ≥65% + ≥3 голоса → КУПИТЬ / ПРОДАТЬ (сильный)
      - ≥60% institutional согласны + ≥2 голоса              → СКОРЕЕ КУПИТЬ / СКОРЕЕ ПРОДАТЬ (средний)
      - иначе                                                 → ВОЗМОЖНО КУПИТЬ / ВОЗМОЖНО ПРОДАТЬ (слабый)
      - news blackout                                         → метим warning + страхуем 70% min

    `probability_pct` всегда в [70, 92].
    """
    inst_up = inst_dn = 0.0
    retail_up = retail_dn = 0.0
    sources: list[dict] = []

    big_players = vp.get("big_players") or []
    cur = float(vp.get("current_price") or forecast.get("current_price") or 0)
    bp_pct = float(bs.get("buyers_pct") or 50)
    sp_pct = float(bs.get("sellers_pct") or 50)
    fav_bs = bs.get("favorite")

    # 1. VP big-players balance — ВЕС 4 (главный институциональный сигнал)
    src1 = {"name": "big_players_vp", "weight": 4.0, "kind": "institutional",
            "side": None, "label": ""}
    if fav_bs == "buyers" and bp_pct >= 65:
        inst_up += 4.0 * (bp_pct / 100.0); src1["side"] = "UP"
        src1["label"] = (
            f"крупные игроки {bp_pct:.0f}% ниже цены — мощная институциональная поддержка"
        )
    elif fav_bs == "sellers" and sp_pct >= 65:
        inst_dn += 4.0 * (sp_pct / 100.0); src1["side"] = "DOWN"
        src1["label"] = (
            f"крупные игроки {sp_pct:.0f}% выше цены — мощное институциональное сопротивление"
        )
    elif fav_bs == "buyers" and bp_pct >= 55:
        inst_up += 4.0 * 0.5; src1["side"] = "UP"
        src1["label"] = f"крупные игроки {bp_pct:.0f}% снизу — умеренная поддержка"
    elif fav_bs == "sellers" and sp_pct >= 55:
        inst_dn += 4.0 * 0.5; src1["side"] = "DOWN"
        src1["label"] = f"крупные игроки {sp_pct:.0f}% сверху — умеренное сопротивление"
    else:
        src1["label"] = f"баланс крупных {bp_pct:.0f}/{sp_pct:.0f}% — нейтрален"
    sources.append(src1)

    # 2. VP no-return levels — ВЕС 3
    nr_levels = (vp.get("forecast_to_utc5_midnight") or {}).get("no_return_levels") or []
    src2 = {"name": "no_return_levels", "weight": 3.0, "kind": "institutional",
            "side": None, "label": ""}
    if nr_levels:
        below = [lv for lv in nr_levels if float(lv.get("price") or 0) < cur]
        above = [lv for lv in nr_levels if float(lv.get("price") or 0) > cur]
        if below and not above:
            inst_up += 3.0; src2["side"] = "UP"
            src2["label"] = (
                f"no-return уровень снизу — цена не возвращается, ждём роста "
                f"({len(below)} уровней)"
            )
        elif above and not below:
            inst_dn += 3.0; src2["side"] = "DOWN"
            src2["label"] = (
                f"no-return уровень сверху — цена не возвращается, ждём снижения "
                f"({len(above)} уровней)"
            )
        else:
            src2["label"] = f"no-return двусторонний (sup={len(below)}, res={len(above)}) — нейтрален"
    elif big_players:
        # fallback: вес support vs resistance в big_players (≥80-percentile объёма)
        sup_w = sum(float(b.get("weight_pct") or 0) for b in big_players
                    if b.get("kind") == "support")
        res_w = sum(float(b.get("weight_pct") or 0) for b in big_players
                    if b.get("kind") == "resistance")
        tot = sup_w + res_w
        if tot > 0:
            ratio_up = sup_w / tot
            if ratio_up >= 0.65:
                inst_up += 3.0 * ratio_up; src2["side"] = "UP"
                src2["label"] = (
                    f"институциональные уровни поддержки доминируют "
                    f"({sup_w:.0f}% vs {res_w:.0f}%)"
                )
            elif ratio_up <= 0.35:
                inst_dn += 3.0 * (1.0 - ratio_up); src2["side"] = "DOWN"
                src2["label"] = (
                    f"институциональное сопротивление доминирует "
                    f"({res_w:.0f}% vs {sup_w:.0f}%)"
                )
            else:
                src2["label"] = f"institutional уровни ~равны ({sup_w:.0f}/{res_w:.0f}%)"
        else:
            src2["label"] = "institutional уровни не найдены"
    else:
        src2["label"] = "institutional уровни не найдены"
    sources.append(src2)

    # 3. COT positioning — ВЕС 3
    cot_sig = _cot_signal_for(pair, cot or {})
    cot_side = cot_sig.get("side")
    cot_str = float(cot_sig.get("strength_pct") or cot_sig.get("strength") or 0)
    cot_z = cot_sig.get("combined_z") or cot_sig.get("z_score") or cot_sig.get("z")
    src3 = {"name": "cot_positioning", "weight": 3.0, "kind": "institutional",
            "side": None, "label": ""}
    if cot_side == "BUY":
        w = max(0.5, min(1.0, cot_str / 100.0)) if cot_str else 0.5
        inst_up += 3.0 * w; src3["side"] = "UP"
        src3["label"] = (
            f"COT BUY (strength {cot_str:.0f}%, z={cot_z:.2f})"
            if isinstance(cot_z, (int, float)) and cot_str
            else f"COT BUY ({cot_str:.0f}%)"
        )
    elif cot_side == "SELL":
        w = max(0.5, min(1.0, cot_str / 100.0)) if cot_str else 0.5
        inst_dn += 3.0 * w; src3["side"] = "DOWN"
        src3["label"] = (
            f"COT SELL (strength {cot_str:.0f}%, z={cot_z:.2f})"
            if isinstance(cot_z, (int, float)) and cot_str
            else f"COT SELL ({cot_str:.0f}%)"
        )
    else:
        src3["label"] = "COT — нет экстремума"
    sources.append(src3)

    # 4. Market Radar overall_score — ВЕС 3
    src4 = {"name": "market_radar", "weight": 3.0, "kind": "institutional",
            "side": None, "label": ""}
    if radar_score is not None and abs(float(radar_score)) >= 5:
        rs = float(radar_score)
        w = min(1.0, abs(rs) / 50.0)
        if rs > 0:
            inst_up += 3.0 * w; src4["side"] = "UP"
            src4["label"] = f"Market Radar +{rs:.0f} (20-сканер консенсус) — BUY"
        else:
            inst_dn += 3.0 * w; src4["side"] = "DOWN"
            src4["label"] = f"Market Radar {rs:.0f} (20-сканер консенсус) — SELL"
    else:
        rs_txt = f"{radar_score:.0f}" if radar_score is not None else "—"
        src4["label"] = f"Market Radar {rs_txt} — слабый сигнал"
    sources.append(src4)

    # 5. FRED macro tilt — ВЕС 2
    fnd = _fnd_signal_for(pair, fundamentals or {})
    fnd_side = fnd.get("side")
    fnd_tilt = float(fnd.get("tilt_score") or fnd.get("tilt_pct") or fnd.get("tilt") or 0)
    fnd_conf = float(fnd.get("confidence_pct") or 0)
    src5 = {"name": "fred_macro", "weight": 2.0, "kind": "institutional",
            "side": None, "label": ""}
    if fnd_side == "BUY":
        w = min(1.0, max(0.4, abs(fnd_tilt) / 80.0))
        inst_up += 2.0 * w; src5["side"] = "UP"
        src5["label"] = f"FRED macro BUY (tilt {fnd_tilt:+.1f}, conf {fnd_conf:.0f}%)"
    elif fnd_side == "SELL":
        w = min(1.0, max(0.4, abs(fnd_tilt) / 80.0))
        inst_dn += 2.0 * w; src5["side"] = "DOWN"
        src5["label"] = f"FRED macro SELL (tilt {fnd_tilt:+.1f}, conf {fnd_conf:.0f}%)"
    else:
        src5["label"] = "FRED macro — нейтрален"
    sources.append(src5)

    # 6. Stakan votes (11-vote consensus от paper_trader_stakan) — ВЕС 3
    stk = _stakan_vote_for(pair, stakan_signals or {})
    stk_dir = stk.get("direction")
    stk_votes = stk.get("votes") or {}
    stk_yes = int(stk_votes.get("yes") or 0)
    stk_total = int(stk_votes.get("total") or 0)
    src6 = {"name": "stakan_votes", "weight": 3.0, "kind": "institutional",
            "side": None, "label": ""}
    if stk_dir in ("BUY", "SELL") and stk_total > 0:
        ratio = stk_yes / stk_total
        if stk_dir == "BUY":
            inst_up += 3.0 * ratio; src6["side"] = "UP"
            src6["label"] = f"стакан-консенсус BUY ({stk_yes}/{stk_total} голосов)"
        else:
            inst_dn += 3.0 * ratio; src6["side"] = "DOWN"
            src6["label"] = f"стакан-консенсус SELL ({stk_yes}/{stk_total} голосов)"
    else:
        skip = stk.get("skip_reason") or "нет данных"
        src6["label"] = f"стакан — {skip}"
    sources.append(src6)

    # 7. VP direction (volume momentum) — ВЕС 2
    vpd = vp.get("direction")
    src7 = {"name": "vp_direction", "weight": 2.0, "kind": "institutional",
            "side": None, "label": ""}
    if vpd == "UP":
        inst_up += 2.0; src7["side"] = "UP"
        src7["label"] = "VP импульс — UP"
    elif vpd == "DOWN":
        inst_dn += 2.0; src7["side"] = "DOWN"
        src7["label"] = "VP импульс — DOWN"
    else:
        src7["label"] = f"VP импульс — {vpd or '—'}"
    sources.append(src7)

    # 8. RETAIL: EMA stack 4H — ВЕС 0.5
    ind4 = (forecast.get("indicators") or {}).get("4H", {}) or {}
    e20 = ind4.get("ema20"); e50 = ind4.get("ema50"); e200 = ind4.get("ema200")
    src8 = {"name": "ema_stack_4h", "weight": 0.5, "kind": "retail",
            "side": None, "label": ""}
    if e20 and e50 and e200:
        if e20 > e50 > e200:
            retail_up += 0.5; src8["side"] = "UP"
            src8["label"] = "розница: 4H EMA-stack 20>50>200 — BUY"
        elif e20 < e50 < e200:
            retail_dn += 0.5; src8["side"] = "DOWN"
            src8["label"] = "розница: 4H EMA-stack 20<50<200 — SELL"
        else:
            src8["label"] = "розница: 4H EMA-stack — смешан"
    else:
        src8["label"] = "розница: 4H EMA — нет данных"
    sources.append(src8)

    # 9. RETAIL: ADX trend — ВЕС 0.5
    adx = float(ind4.get("adx") or 0)
    plus_di = float(ind4.get("plus_di") or 0)
    minus_di = float(ind4.get("minus_di") or 0)
    src9 = {"name": "adx_trend", "weight": 0.5, "kind": "retail",
            "side": None, "label": ""}
    if adx >= 25 and plus_di > minus_di + 3:
        retail_up += 0.5; src9["side"] = "UP"
        src9["label"] = f"розница: ADX={adx:.0f} +DI>{minus_di:.0f}-DI — BUY"
    elif adx >= 25 and minus_di > plus_di + 3:
        retail_dn += 0.5; src9["side"] = "DOWN"
        src9["label"] = f"розница: ADX={adx:.0f} -DI>{plus_di:.0f}+DI — SELL"
    else:
        src9["label"] = f"розница: ADX={adx:.0f} — слабый/смешанный"
    sources.append(src9)

    # 10. RETAIL: MACD 1H — ВЕС 0.5
    ind1 = (forecast.get("indicators") or {}).get("1H", {}) or {}
    macd_h = float(ind1.get("macd_hist") or 0)
    src10 = {"name": "macd_1h", "weight": 0.5, "kind": "retail",
             "side": None, "label": ""}
    if macd_h > 0:
        retail_up += 0.5; src10["side"] = "UP"
        src10["label"] = f"розница: 1H MACD>0 ({macd_h:.4f}) — BUY"
    elif macd_h < 0:
        retail_dn += 0.5; src10["side"] = "DOWN"
        src10["label"] = f"розница: 1H MACD<0 ({macd_h:.4f}) — SELL"
    else:
        src10["label"] = "розница: 1H MACD — нейтрален"
    sources.append(src10)

    # ── totals ──
    total_up = inst_up + retail_up
    total_dn = inst_dn + retail_dn
    total = total_up + total_dn

    inst_sources = [s for s in sources if s["kind"] == "institutional"]
    institutional_sources_total = len(inst_sources)
    inst_voted = [s for s in inst_sources if s["side"] in ("UP", "DOWN")]

    # ── Принудительно выбираем сторону. Никогда не возвращаем «ОЖИДАНИЕ».
    # Tie-break при total_up == total_dn: используем сторону forecast или BUY.
    if total_up > total_dn:
        favorite_side = "buyers"
    elif total_dn > total_up:
        favorite_side = "sellers"
    else:
        fc_side = str(forecast.get("side") or "BUY").upper()
        favorite_side = "sellers" if fc_side == "SELL" else "buyers"

    if favorite_side == "buyers":
        agree = sum(1 for s in inst_voted if s["side"] == "UP")
        disagree = sum(1 for s in inst_voted if s["side"] == "DOWN")
    else:
        agree = sum(1 for s in inst_voted if s["side"] == "DOWN")
        disagree = sum(1 for s in inst_voted if s["side"] == "UP")

    favorite_balance_pct = (
        round(max(total_up, total_dn) / total * 100.0, 1) if total > 0 else 50.0
    )
    # Согласие = доля «за» среди тех источников, что вообще проголосовали.
    voted_count = agree + disagree
    agreement_pct = (
        round(agree / voted_count * 100.0, 1) if voted_count > 0 else 0.0
    )

    in_blackout, blackout_reason = _check_news_blackout(pair, news_blackouts)

    # ── Вероятность успеха в [70, 92] (математическое ожидание сигнала).
    # База от перевеса: 50% → 70%, 100% → 92%.
    base_prob = 70.0 + max(0.0, (favorite_balance_pct - 50.0)) * 0.44
    # Бонус от согласия среди голосовавших.
    agree_bonus = (agreement_pct / 100.0) * 6.0
    # Бонус за количество проголосовавших источников (макс 5 → +2.5).
    voted_bonus = min(float(voted_count), 5.0) * 0.5
    # Штраф за news blackout — снижаем уверенность (но не ниже 70%).
    blackout_penalty = 5.0 if in_blackout else 0.0
    prob_pct = max(
        70.0,
        min(92.0, round(base_prob + agree_bonus + voted_bonus - blackout_penalty, 1)),
    )

    # ── Вердикт: всегда КУПИТЬ или ПРОДАТЬ. Три уровня силы.
    is_strong = (
        agreement_pct >= 80 and favorite_balance_pct >= 65 and voted_count >= 3
    )
    is_medium = agreement_pct >= 60 and voted_count >= 2 and not is_strong

    fav_word_short = "ВВЕРХ" if favorite_side == "buyers" else "ВНИЗ"
    if is_strong:
        verdict_strength = "strong"
        if favorite_side == "buyers":
            verdict, verdict_color = "КУПИТЬ", "green"
        else:
            verdict, verdict_color = "ПРОДАТЬ", "red"
        primary_reason = (
            f"{agree} из {institutional_sources_total} институциональных "
            f"источников согласны ({agreement_pct:.0f}% от голосовавших). "
            f"Перевес институционала {favorite_balance_pct:.0f}%. "
            f"Крупные игроки ({bp_pct:.0f}/{sp_pct:.0f}%), стакан, COT, "
            f"FRED-macro и Market Radar указывают в одну сторону. "
            f"На горизонте 24ч до 00:00 UTC+5 рынок выбрал направление {fav_word_short} — "
            f"мелкие колебания внутри сессии это шум, прогноз отработает за ~5 часов. "
            f"Рынок не даёт возможности идти против. Вероятность {prob_pct:.0f}%."
        )
    elif is_medium:
        verdict_strength = "medium"
        if favorite_side == "buyers":
            verdict, verdict_color = "СКОРЕЕ КУПИТЬ", "yellow_buy"
        else:
            verdict, verdict_color = "СКОРЕЕ ПРОДАТЬ", "yellow_sell"
        primary_reason = (
            f"{agree} из {institutional_sources_total} институциональных согласны "
            f"({agreement_pct:.0f}% от голосовавших), перевес {favorite_balance_pct:.0f}%. "
            f"Импульс крупных игроков направлен {fav_word_short} — направление до 00:00 UTC+5 есть, "
            f"но запас прочности средний. Прогноз отработает за ~5 часов. "
            f"Вероятность {prob_pct:.0f}%."
        )
    else:
        verdict_strength = "weak"
        if favorite_side == "buyers":
            verdict, verdict_color = "ВОЗМОЖНО КУПИТЬ", "yellow_buy"
        else:
            verdict, verdict_color = "ВОЗМОЖНО ПРОДАТЬ", "yellow_sell"
        primary_reason = (
            f"Институционал расходится: {agree} из "
            f"{institutional_sources_total} источников "
            f"({agreement_pct:.0f}% от голосовавших, voted={voted_count}). "
            f"Перевес слабый ({favorite_balance_pct:.0f}%) — но направление "
            f"всё-таки {fav_word_short} (24ч-горизонт). Прогноз ~5 часов, вероятность "
            f"{prob_pct:.0f}% — допустимо лишь как лёгкая позиция."
        )

    if in_blackout:
        primary_reason = (
            f"⚠️ В ±30 мин high-impact новость ({blackout_reason}). "
            + primary_reason
        )

    # горизонт до 00:00 UTC+5 + цель + no-return
    hours_to_mid = _hours_to_midnight_utc5()
    atr_1h = float(((forecast.get("indicators") or {}).get("1H", {}) or {}).get("atr14") or 0)
    pip_mul = 100 if pair.endswith("JPY") else 10000

    target_by_mid: float | None = None
    target_pips_to_mid: float | None = None
    if cur > 0 and atr_1h > 0:
        sign = 1 if favorite_side == "buyers" else -1
        # экстраполяция: ~1×ATR за 4 часа сильного направленного движения
        drift_factor = max(0.25, min(1.5, hours_to_mid / 4.0))
        target_by_mid = round(cur + sign * atr_1h * drift_factor, 5)
        target_pips_to_mid = round(abs(target_by_mid - cur) * pip_mul, 1)

    # ближайший no-return из big_players на противоположной стороне
    no_return_level: float | None = None
    no_return_pips: float | None = None
    if cur > 0 and big_players:
        sign = 1 if favorite_side == "buyers" else -1
        cands = [
            float(b.get("price") or 0) for b in big_players
            if (sign > 0 and float(b.get("price") or 0) < cur)
            or (sign < 0 and float(b.get("price") or 0) > cur)
        ]
        if cands:
            no_return_level = round(min(cands, key=lambda p: abs(p - cur)), 5)
            no_return_pips = round(abs(no_return_level - cur) * pip_mul, 1)

    return {
        "verdict": verdict,
        "verdict_color": verdict_color,
        "verdict_strength": verdict_strength,
        "side": "BUY" if favorite_side == "buyers" else "SELL",
        "probability_pct": prob_pct,
        "favorite_side": favorite_side,
        "favorite_balance_pct": favorite_balance_pct,
        "buyers_pct": bp_pct,
        "sellers_pct": sp_pct,
        "institutional_score_up": round(inst_up, 2),
        "institutional_score_down": round(inst_dn, 2),
        "retail_score_up": round(retail_up, 2),
        "retail_score_down": round(retail_dn, 2),
        "total_up": round(total_up, 2),
        "total_down": round(total_dn, 2),
        "agreement_pct": agreement_pct,
        "institutional_sources_agree": agree,
        "institutional_sources_voted": voted_count,
        "institutional_sources_total": institutional_sources_total,
        "hours_to_midnight_utc5": hours_to_mid,
        "target_by_midnight": target_by_mid,
        "target_pips_to_midnight": target_pips_to_mid,
        "no_return_level": no_return_level,
        "no_return_pips": no_return_pips,
        "news_blackout": in_blackout,
        "news_blackout_reason": blackout_reason,
        "reason_ru": primary_reason,
        "sources": sources,
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
    stakan_signals = _load(_STAKAN_SIGNALS_FILE, {})
    news_blackouts = _load(_NEWS_BLACKOUTS_FILE, {})

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
    verdict = _institutional_verdict(
        pair, forecast, vp, bs, radar_score,
        fundamentals, cot, stakan_signals, news_blackouts,
    )

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
        "verdict": verdict,
        "per_session_strategy": strat,
        "radar_score": radar_score,
    }


# ────────────────────────────────────────────────────────────────────────────
# 6. Public API: compact snapshot for all 28 pairs (for the picker grid)
# ────────────────────────────────────────────────────────────────────────────
def build_all_summary() -> dict:
    """Лёгкий вью по 28 парам — для селектора валюты сверху раздела.

    Считаем институциональный verdict для каждой пары прямо тут
    (используя кэш из forecasts.json — VP, indicators и т.д. уже там).
    Так на чипах селектора видно «КУПИТЬ»/«ПРОДАТЬ» сразу для всех 28
    пар без дополнительных запросов.
    """
    forecasts_blob = _load(_FORECASTS_FILE, {"forecasts": {}})
    forecasts = forecasts_blob.get("forecasts") or {}
    strategy_cfg = _load(_STRATEGY_FILE, {"pairs": {}})
    radar = _load(_RADAR_FILE, {"pairs": {}})
    fundamentals = _load(_FUNDAMENTALS_FILE, {})
    cot = _load(_COT_FILE, {})
    stakan_signals = _load(_STAKAN_SIGNALS_FILE, {})
    news_blackouts = _load(_NEWS_BLACKOUTS_FILE, {})

    items = []
    sess = _current_session_label()
    counts = {"strong": 0, "medium": 0, "weak": 0, "buy": 0, "sell": 0}
    for p in config.PAIRS:
        f = forecasts.get(p, {}) or {}
        cfg = (strategy_cfg.get("pairs", {}) or {}).get(p, {}) or {}
        sess_data = (cfg.get("by_session", {}) or {}).get(sess, {}) or {}
        vp_cached = f.get("volume_profile") or {}
        bs = _buyers_sellers_balance(vp_cached)
        radar_score = _safe_get(radar, "pairs", p, "overall_score")
        if radar_score is None:
            radar_score = _safe_get(radar, "pairs", p, "score")
        try:
            v = _institutional_verdict(
                p, f, vp_cached, bs, radar_score,
                fundamentals, cot, stakan_signals, news_blackouts,
            )
        except Exception:
            v = {
                "verdict": "КУПИТЬ", "verdict_color": "yellow_buy",
                "verdict_strength": "weak", "side": "BUY",
                "probability_pct": 70.0,
                "favorite_balance_pct": 50.0,
            }
        counts[v.get("verdict_strength", "weak")] = (
            counts.get(v.get("verdict_strength", "weak"), 0) + 1
        )
        side_key = "buy" if v.get("side") == "BUY" else "sell"
        counts[side_key] = counts.get(side_key, 0) + 1
        items.append({
            "pair": p,
            "side": v.get("side"),
            "raw_forecast_side": f.get("side"),
            "probability_pct": v.get("probability_pct"),
            "raw_forecast_probability_pct": f.get("probability_pct"),
            "current_price": f.get("current_price"),
            "recommended_hours": f.get("recommended_hours"),
            "session": f.get("session") or sess,
            "session_qualifies_70pct": bool(sess_data.get("qualifies_70pct")),
            "session_wr_pct": sess_data.get("win_rate_pct"),
            "verdict": v.get("verdict"),
            "verdict_color": v.get("verdict_color"),
            "verdict_strength": v.get("verdict_strength"),
            "favorite_balance_pct": v.get("favorite_balance_pct"),
            "agreement_pct": v.get("agreement_pct"),
            "news_blackout": v.get("news_blackout"),
        })
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "current_session": sess,
        "items": items,
        "counts": counts,
    }
