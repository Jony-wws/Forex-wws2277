"""free_signals — дополнительные источники сигналов для meta-strategy ensemble.

Все источники бесплатные (Yahoo Finance + вычисления). НЕ требуют API ключей.
Используются strategy_meta_agent для расширения ансамбля QUALIFIED ячеек:

1. **currency_strength_matrix** — относительная сила каждой из 8 валют по
   1h-возвратам за 5 дней. Если JPY слаба у 5+ пар, любая sell-JPY пара
   получает +1 bias (confluence).
2. **dxy_trend_signal** — тренд индекса доллара (DXY = $DX-Y.NYB через
   Yahoo). Если DXY вверх и пара содержит USD как base — bias toward
   USD-base (+ для XXXUSD это означает SELL).
3. **atr_regime_classifier** — текущий 24ч ATR vs 60д медианный ATR.
   Low-vol regime → reversal-варианты надёжнее. High-vol → momentum.
   Возвращает bonus к Wilson если variant соответствует regime.
4. **cross_pair_confluence** — для JPY-пары: если все 4 (USDJPY, EURJPY,
   GBPJPY, AUDJPY) идут в одну сторону за последние 24ч, +1 bonus.
5. **volume_profile_distance** — расстояние текущей цены до VAH/VAL.
   Если цена близко к VAH (≤ 0.2 ATR) и сигнал SELL — +1 bias.

Всё вычисляется ПОСЛЕ bulk Yahoo fetch, без дополнительных HTTP-запросов
(кроме DXY который тянется один раз отдельно).
"""
from __future__ import annotations
import logging
import time
from typing import Optional

import pandas as pd

from . import config

log = logging.getLogger("free_signals")


# ─────────────────────── currency strength ───────────────────────

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]


def _split_pair(pair: str) -> tuple[str, str]:
    """EURUSD -> (EUR, USD). Все наши 28 пар имеют 6 символов."""
    return pair[:3], pair[3:6]


def compute_currency_strength_matrix(
    bulk_data: dict[str, pd.DataFrame],
    lookback_bars: int = 24,
) -> dict[str, float]:
    """Считает индекс силы каждой из 8 валют по 1h-возвратам.

    Логика: для каждой пары XXXYYY, лог-доходность за last `lookback_bars` =
    log(close[-1] / close[-lookback]). Если pair выросла, base сильнее (XXX
    +возврат), quote слабее (YYY -возврат). Усредняем по всем парам где
    валюта упоминается → индекс силы (-100 .. +100).

    Возвращает dict {currency: strength_score}.
    Currency strength = средний log-return по парам где currency = base
    минус средний log-return по парам где currency = quote, нормализован
    в шкалу ~-100 .. +100.
    """
    accum: dict[str, list[float]] = {c: [] for c in CURRENCIES}
    for pair, df in (bulk_data or {}).items():
        if df is None or df.empty or "Close" not in df.columns:
            continue
        if len(df) < lookback_bars + 1:
            continue
        try:
            c0 = float(df["Close"].iloc[-lookback_bars])
            c1 = float(df["Close"].iloc[-1])
            if c0 <= 0 or c1 <= 0:
                continue
            ret = (c1 / c0) - 1.0
        except Exception:
            continue
        base, quote = _split_pair(pair)
        if base in accum:
            accum[base].append(ret)
        if quote in accum:
            accum[quote].append(-ret)
    out: dict[str, float] = {}
    for cur, rets in accum.items():
        if not rets:
            out[cur] = 0.0
            continue
        avg = sum(rets) / len(rets)
        # типичный 24h ret для FX ±0.5% → масштабирующий 200×
        out[cur] = round(max(-100.0, min(100.0, avg * 20000)), 2)
    return out


def pair_strength_signal(pair: str, strength: dict[str, float]) -> Optional[dict]:
    """Возвращает bias-сигнал для одной пары на основе матрицы силы.

    Логика: bias = strength[base] - strength[quote]. Если bias > 25,
    пара явно идёт вверх (BUY). Если < -25, явно вниз (SELL).
    """
    if not strength:
        return None
    base, quote = _split_pair(pair)
    if base not in strength or quote not in strength:
        return None
    diff = strength[base] - strength[quote]
    pts = 0
    side = None
    if diff >= 50:
        pts = 2
        side = "BUY"
    elif diff >= 25:
        pts = 1
        side = "BUY"
    elif diff <= -50:
        pts = -2
        side = "SELL"
    elif diff <= -25:
        pts = -1
        side = "SELL"
    if pts == 0:
        return None
    return {
        "name": "currency_strength",
        "side": side,
        "diff": round(diff, 1),
        "base_strength": strength[base],
        "quote_strength": strength[quote],
        "pts": pts,
    }


# ─────────────────────── DXY trend signal ───────────────────────

_DXY_CACHE: dict = {"ts": 0, "df": None}


def fetch_dxy_data() -> Optional[pd.DataFrame]:
    """Тянем индекс доллара (DXY, $DX-Y.NYB) на Yahoo за 30d 1h.

    Кэшируем на 10 минут, чтобы не бить Yahoo каждый sweep.
    Возвращает DataFrame с колонкой Close или None если Yahoo вернул empty.
    """
    now = time.time()
    if _DXY_CACHE["df"] is not None and (now - _DXY_CACHE["ts"]) < 600:
        return _DXY_CACHE["df"]
    try:
        import yfinance as yf
        df = yf.download(
            "DX-Y.NYB",
            interval="1h",
            period="30d",
            progress=False,
            auto_adjust=False,
            prepost=False,
        )
    except Exception as e:
        log.warning(f"DXY fetch failed: {e}")
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    _DXY_CACHE["ts"] = now
    _DXY_CACHE["df"] = df
    return df


def pair_dxy_signal(pair: str, dxy_df: Optional[pd.DataFrame],
                    lookback_bars: int = 24) -> Optional[dict]:
    """DXY-trend bias только для пар с USD.

    Если DXY +0.3% за 24ч и пара = XXXUSD, USD сильнее → SELL для XXXUSD.
    Если пара = USDXXX (USDJPY, USDCHF), USD сильнее → BUY.
    Если пара не содержит USD — skip.
    """
    if dxy_df is None or dxy_df.empty or "Close" not in dxy_df.columns:
        return None
    if len(dxy_df) < lookback_bars + 1:
        return None
    base, quote = _split_pair(pair)
    if "USD" not in (base, quote):
        return None
    try:
        c0 = float(dxy_df["Close"].iloc[-lookback_bars])
        c1 = float(dxy_df["Close"].iloc[-1])
        if c0 <= 0:
            return None
        dxy_ret_pct = (c1 / c0 - 1.0) * 100.0
    except Exception:
        return None

    # пороги: ±0.3% / ±0.6% за 24ч на DXY = умеренный/сильный тренд
    abs_ret = abs(dxy_ret_pct)
    if abs_ret < 0.3:
        return None
    direction = "USD_UP" if dxy_ret_pct > 0 else "USD_DOWN"
    pts_abs = 1 if abs_ret < 0.6 else 2
    if direction == "USD_UP":
        # USD сильнее → BUY USDXXX, SELL XXXUSD
        side = "BUY" if base == "USD" else "SELL"
    else:
        side = "SELL" if base == "USD" else "BUY"
    pts = pts_abs if side == "BUY" else -pts_abs
    return {
        "name": "dxy_trend",
        "side": side,
        "dxy_ret_24h_pct": round(dxy_ret_pct, 3),
        "direction": direction,
        "pts": pts,
    }


# ─────────────────────── ATR regime classifier ───────────────────────


def pair_atr_regime(snapshots: list, lookback_recent: int = 24,
                    lookback_baseline: int = 480) -> Optional[dict]:
    """Классифицирует текущий volatility-regime пары.

    snapshots — list[(ts, close, ind_4h, ind_1h, ind_15m)] из meta-agent.
    Сравнивает медианный 1h ATR за последние 24h vs за последние 20 дней.
    Возвращает {'regime': 'LOW'/'NORMAL'/'HIGH', 'ratio': float, 'pts': 0}.

    Это ConfBonus-источник (не bias-источник): pts всегда 0, но bonus +1
    если variant соответствует regime (см. docstring модуля).
    """
    if not snapshots or len(snapshots) < lookback_baseline:
        return None
    recent_atrs: list[float] = []
    for ts, close, _i4h, ind_1h, _i15m in snapshots[-lookback_recent:]:
        if ind_1h and "atr14" in ind_1h:
            try:
                v = float(ind_1h["atr14"])
                if v > 0:
                    recent_atrs.append(v)
            except Exception:
                continue
    baseline_atrs: list[float] = []
    for ts, close, _i4h, ind_1h, _i15m in snapshots[-lookback_baseline:-lookback_recent]:
        if ind_1h and "atr14" in ind_1h:
            try:
                v = float(ind_1h["atr14"])
                if v > 0:
                    baseline_atrs.append(v)
            except Exception:
                continue
    if not recent_atrs or not baseline_atrs:
        return None
    recent_med = sorted(recent_atrs)[len(recent_atrs) // 2]
    baseline_med = sorted(baseline_atrs)[len(baseline_atrs) // 2]
    if baseline_med <= 0:
        return None
    ratio = recent_med / baseline_med
    if ratio < 0.7:
        regime = "LOW"
    elif ratio > 1.4:
        regime = "HIGH"
    else:
        regime = "NORMAL"
    return {
        "name": "atr_regime",
        "regime": regime,
        "ratio": round(ratio, 2),
        "recent_atr": round(recent_med, 5),
        "baseline_atr": round(baseline_med, 5),
        "pts": 0,
    }


# ─────────────────────── cross-pair JPY confluence ───────────────────────


def jpy_confluence_signal(pair: str, bulk_data: dict[str, pd.DataFrame],
                          lookback_bars: int = 24) -> Optional[dict]:
    """Если пара содержит JPY и ВСЕ 4 главные JPY-пары идут в одну сторону
    за последние 24h — это confluence-сигнал на JPY.

    Главные JPY-пары: USDJPY, EURJPY, GBPJPY, AUDJPY.
    Если все 4 +0.2%+ → JPY слабый → +1 на любой XXXJPY (SELL JPY)
    Если все 4 -0.2%- → JPY сильный → +1 на любой JPYXXX (BUY JPY)
    """
    if "JPY" not in pair or not bulk_data:
        return None
    main_jpy = ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY"]
    rets: list[float] = []
    for jp in main_jpy:
        df = bulk_data.get(jp)
        if df is None or df.empty or "Close" not in df.columns:
            continue
        if len(df) < lookback_bars + 1:
            continue
        try:
            c0 = float(df["Close"].iloc[-lookback_bars])
            c1 = float(df["Close"].iloc[-1])
            if c0 > 0:
                rets.append((c1 / c0 - 1.0) * 100.0)
        except Exception:
            continue
    if len(rets) < 3:
        return None
    avg_pct = sum(rets) / len(rets)
    all_up = all(r > 0.05 for r in rets)
    all_down = all(r < -0.05 for r in rets)
    if not (all_up or all_down):
        return None
    base, quote = _split_pair(pair)
    if all_up:
        # JPY weakening — SELL JPY (i.e. BUY XXXJPY, SELL JPYXXX)
        side = "BUY" if quote == "JPY" else "SELL"
    else:
        side = "SELL" if quote == "JPY" else "BUY"
    pts = 1 if side == "BUY" else -1
    return {
        "name": "jpy_confluence",
        "side": side,
        "avg_24h_ret_pct": round(avg_pct, 3),
        "all_up": all_up,
        "all_down": all_down,
        "pts": pts,
    }


# ─────────────────────── volume profile distance ───────────────────────


def pair_vp_distance_signal(pair: str) -> Optional[dict]:
    """Расстояние текущей цены до VAH/VAL volume profile.

    Читает state/agent_specialist_{pair}.json (там per-pair VP в summary).
    Если цена близко к VAH (≤ 0.3 × atr14) — sell-bias.
    Если близко к VAL — buy-bias.
    """
    import json
    import os
    p = config.STATE_DIR / f"agent_specialist_{pair}.json"
    if not os.path.exists(p):
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    s = (d or {}).get("summary") or {}
    vp = s.get("volume_profile") or s.get("vp") or {}
    vah = vp.get("vah")
    val = vp.get("val")
    cur = s.get("current_price") or s.get("price")
    atr = s.get("atr14") or s.get("atr") or 0
    if not (vah and val and cur and atr):
        return None
    try:
        vah, val, cur, atr = float(vah), float(val), float(cur), float(atr)
    except Exception:
        return None
    if atr <= 0:
        return None
    dist_to_vah = abs(cur - vah) / atr
    dist_to_val = abs(cur - val) / atr
    pts = 0
    side = None
    if dist_to_vah <= 0.3 and cur < vah * 1.005:
        # near VAH from below — SELL bias
        side = "SELL"
        pts = -1
    elif dist_to_val <= 0.3 and cur > val * 0.995:
        # near VAL from above — BUY bias
        side = "BUY"
        pts = 1
    if pts == 0:
        return None
    return {
        "name": "vp_distance",
        "side": side,
        "dist_to_vah_atr": round(dist_to_vah, 2),
        "dist_to_val_atr": round(dist_to_val, 2),
        "pts": pts,
    }
