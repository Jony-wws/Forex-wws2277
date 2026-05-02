"""market_radar — «военный радар рынка» (запрос пользователя 2026-05-01).

Запускается как отдельный child-процесс под orchestrator. Каждые
RADAR_INTERVAL_SEC секунд независимо считает 15+ независимых сканеров для всех
28 пар и пишет консолидированный score в state/market_radar.json.

Каждый сканер возвращает dict {pair → {score: -100..+100, label: str, ...}},
где знак score — направление (положительный = BUY-bias, отрицательный = SELL-bias),
модуль — сила сигнала. Финальный «overall_score» — взвешенная сумма всех 15+
сканеров (weight=1 у каждого по умолчанию). Голос «pass» считается если
overall_score проходит нейтральный порог.

Сканеры специально независимы и без перекрёстных вызовов — каждый ловит
exception самостоятельно. Если один источник упал — остальные продолжают.

Цитата пользователя:
> «У военных есть же свой сканер своя защита. У нас тоже должно быть. Он
> должен показать всё. Одну вещь должен показать всё абсолютно 10 15 20
> 40 100 функций на одном методе будет и у каждого будет своя задача а
> потом система будет видеть всё это и давать общую оценку».

Этот модуль — реализация именно этого: 15+ функций × 28 пар = 420+ оценок,
агрегируются в одну общую таблицу.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .data import yahoo
from . import indicators as ind
from . import volume_profile as vp_mod

log = logging.getLogger("market_radar")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "market_radar.log"),
        logging.StreamHandler(),
    ],
)

RADAR_FILE = config.STATE_DIR / "market_radar.json"
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_market_radar.json"
RADAR_INTERVAL_SEC = 60

NEUTRAL_THRESHOLD = 10        # |score| < 10 → нейтрально
SCANNER_PASS_THRESHOLD = 30   # |score| >= 30 → сигнал считается «уверенным»


# ─────────────────────────────────────────────────────────────────────
#                          ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat() -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "market_radar",
        "ts": _now().isoformat(),
        "pid": __import__("os").getpid(),
    }))


def _safe_float(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        if np.isnan(v) or np.isinf(v):
            return default
        return v
    except Exception:
        return default


def _zscore(arr: np.ndarray) -> float:
    arr = arr[~np.isnan(arr)]
    if len(arr) < 5:
        return 0.0
    m, s = np.mean(arr), np.std(arr)
    if s <= 0:
        return 0.0
    return float((arr[-1] - m) / s)


def _pip(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def _decompose(pair: str) -> tuple[str, str]:
    return pair[:3], pair[3:]


def _empty_score(label: str = "—") -> dict:
    return {"score": 0.0, "label": label}


# Каждый сканер: (pair, **ctx) → {"score": float [-100..100], "label": str, ...}
# Все 28 пар обрабатываются в цикле.

# ─────────────────────────────────────────────────────────────────────
#                                СКАНЕРЫ
# ─────────────────────────────────────────────────────────────────────

def s01_currency_strength(pair: str, **ctx) -> dict:
    """Ранг силы базы и квоты по 24h движению. Если база сильнее → BUY."""
    strength = ctx.get("currency_strength")
    if not strength:
        return _empty_score("no strength data")
    base, quote = _decompose(pair)
    sb = strength.get(base, 0.0)
    sq = strength.get(quote, 0.0)
    diff = sb - sq  # положительно → база сильнее → BUY
    score = float(np.clip(diff * 50, -100, 100))
    return {"score": score, "label": f"{base}={sb:+.2f} vs {quote}={sq:+.2f}"}


def s02_correlation_divergence(pair: str, **ctx) -> dict:
    """Спред между парой и её главным «коррелированным братом» относительно 30d
    среднего. EURUSD vs GBPUSD, USDJPY vs USDCHF и т.п. Большое расхождение
    (z-score) — сильный сигнал на схождение."""
    sibling_map = {
        "EURUSD": "GBPUSD", "GBPUSD": "EURUSD",
        "USDCHF": "USDJPY", "USDJPY": "USDCHF",
        "AUDUSD": "NZDUSD", "NZDUSD": "AUDUSD",
        "USDCAD": "USDCHF",
        "EURJPY": "GBPJPY", "GBPJPY": "EURJPY",
        "EURGBP": "EURUSD",
    }
    sibling = sibling_map.get(pair)
    if not sibling:
        return _empty_score("no sibling")
    try:
        a = yahoo.latest_bars(pair, "1h", 24 * 30)
        b = yahoo.latest_bars(sibling, "1h", 24 * 30)
        if a is None or b is None or a.empty or b.empty:
            return _empty_score("no data")
        # ratio normalized to first
        ra = a["Close"].to_numpy() / a["Close"].iloc[0]
        rb = b["Close"].to_numpy() / b["Close"].iloc[0]
        n = min(len(ra), len(rb))
        spread = ra[:n] - rb[:n]
        z = _zscore(spread)
        # большой положительный z → пара переоценена относительно sibling → ожидаем coverage → SELL
        score = float(np.clip(-z * 30, -100, 100))
        return {"score": score, "label": f"vs {sibling} z={z:+.2f}"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s03_triangular_arb(pair: str, **ctx) -> dict:
    """Триангуляционный сигнал: implied EUR/USD = (EUR/GBP) × (GBP/USD).
    Если фактический отличается от implied — мини-сигнал на возврат."""
    triples = {
        "EURUSD": ("EURGBP", "GBPUSD"),
        "EURJPY": ("EURUSD", "USDJPY"),
        "GBPJPY": ("GBPUSD", "USDJPY"),
        "AUDJPY": ("AUDUSD", "USDJPY"),
        "EURCHF": ("EURUSD", "USDCHF"),
        "GBPCHF": ("GBPUSD", "USDCHF"),
        "AUDCAD": ("AUDUSD", "USDCAD"),
    }
    if pair not in triples:
        return _empty_score("no triangle")
    a, b = triples[pair]
    try:
        pa = yahoo.latest_price(a)
        pb = yahoo.latest_price(b)
        pp = yahoo.latest_price(pair)
        if not all([pa, pb, pp]):
            return _empty_score("no live prices")
        # Для триплов вида X/Y = (X/Z) × (Z/Y) — но для чужих ориентировок (EURJPY):
        # EURJPY ≈ EURUSD × USDJPY (это простой случай)
        implied = pa * pb
        # Для EURUSD ≈ EURGBP × GBPUSD — тоже работает
        diff = (pp - implied) / implied
        # положительное diff → актуальная цена выше implied → SELL
        score = float(np.clip(-diff * 5000, -100, 100))
        return {"score": score, "label": f"impl={implied:.5f} act={pp:.5f} d={diff*100:+.3f}%"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s04_volatility_regime(pair: str, **ctx) -> dict:
    """ATR_1h сейчас vs 20d median. Низкая → надвигается ход. Высокая → осторожно."""
    try:
        bars = yahoo.latest_bars(pair, "1h", 24 * 30)
        if bars is None or bars.empty:
            return _empty_score("no data")
        atr = ind.atr(bars, 14).dropna()
        if atr.empty:
            return _empty_score("no atr")
        cur = float(atr.iloc[-1])
        med = float(atr.median())
        ratio = cur / med if med > 0 else 1.0
        # ratio < 0.7 (squeeze) → нейтральный сигнал «надвигается ход» — score близок к 0
        # ratio > 1.5 → шум — также 0
        # сам по себе режим не направление, но подавляем сигнал других при экстриме
        if 0.85 <= ratio <= 1.20:
            score = 0
            label = f"normal vol (×{ratio:.2f})"
        elif ratio < 0.85:
            score = 0
            label = f"squeeze (×{ratio:.2f}) — breakout coming"
        else:
            score = 0
            label = f"elevated vol (×{ratio:.2f})"
        return {"score": score, "label": label, "ratio": round(ratio, 3)}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s05_session_momentum(pair: str, **ctx) -> dict:
    """Тек. сессия (Asia/London/Overlap/NY) — куда движется текущий бар vs
    open текущей сессии. Сильный гэп в одну сторону."""
    h = _now().hour
    if h < 7:
        sess_start_h = 0
    elif h < 13:
        sess_start_h = 7
    elif h < 17:
        sess_start_h = 13
    else:
        sess_start_h = 17
    try:
        bars = yahoo.latest_bars(pair, "1h", 24)
        if bars is None or bars.empty:
            return _empty_score("no data")
        now = _now()
        sess_start = now.replace(hour=sess_start_h, minute=0, second=0, microsecond=0)
        bars_idx = pd.to_datetime(bars.index, utc=True)
        mask = bars_idx >= sess_start
        sess = bars.loc[mask]
        if len(sess) < 2:
            return _empty_score("session too short")
        chg_pct = (sess["Close"].iloc[-1] - sess["Open"].iloc[0]) / sess["Open"].iloc[0]
        score = float(np.clip(chg_pct * 5000, -100, 100))
        return {"score": score, "label": f"sess Δ={chg_pct*100:+.3f}%"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s06_hour_of_day_bias(pair: str, **ctx) -> dict:
    """История WIN/LOSS на текущий час по closed_trades.json (если есть данные)."""
    closed = ctx.get("closed_trades", [])
    if not closed:
        return _empty_score("no history")
    h = _now().hour
    bucket = [t for t in closed if t.get("pair") == pair and t.get("open_time")
              and datetime.fromisoformat(t["open_time"]).hour == h]
    if len(bucket) < 5:
        return _empty_score(f"only {len(bucket)} samples")
    wins = sum(1 for t in bucket if t.get("result") == "WIN")
    wr = wins / len(bucket)
    # WR > 0.6 → BUY-bias по истории; < 0.4 → SELL
    score = float(np.clip((wr - 0.5) * 200, -100, 100))
    return {"score": score, "label": f"WR@h{h}={wr*100:.0f}% (n={len(bucket)})"}


def s07_dow_bias(pair: str, **ctx) -> dict:
    """История WR по дню недели."""
    closed = ctx.get("closed_trades", [])
    if not closed:
        return _empty_score("no history")
    dow = _now().weekday()
    bucket = [t for t in closed if t.get("pair") == pair and t.get("open_time")
              and datetime.fromisoformat(t["open_time"]).weekday() == dow]
    if len(bucket) < 5:
        return _empty_score(f"only {len(bucket)} samples")
    wins = sum(1 for t in bucket if t.get("result") == "WIN")
    wr = wins / len(bucket)
    score = float(np.clip((wr - 0.5) * 200, -100, 100))
    return {"score": score, "label": f"WR@dow{dow}={wr*100:.0f}% (n={len(bucket)})"}


def s08_pivot_distance(pair: str, **ctx) -> dict:
    """Дист. до классических pivot point R/S уровней (D1)."""
    try:
        bars = yahoo.latest_bars(pair, "1d", 5)
        if bars is None or bars.empty or len(bars) < 2:
            return _empty_score("no data")
        prev = bars.iloc[-2]
        h, l, c = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
        pp = (h + l + c) / 3
        r1 = 2 * pp - l
        s1 = 2 * pp - h
        cur = float(bars["Close"].iloc[-1])
        atr = ind.atr(bars, 3).dropna()
        if atr.empty:
            return _empty_score("no atr")
        atr_v = float(atr.iloc[-1])
        if atr_v <= 0:
            return _empty_score("zero atr")
        # цена близко к S1 → BUY (отскок вверх); близко к R1 → SELL
        d_s1 = (cur - s1) / atr_v
        d_r1 = (r1 - cur) / atr_v
        if d_s1 < 0.5:
            score = 60
            label = f"near S1={s1:.5f} (Δ{d_s1:.2f}ATR)"
        elif d_r1 < 0.5:
            score = -60
            label = f"near R1={r1:.5f} (Δ{d_r1:.2f}ATR)"
        elif cur < pp:
            score = -20
            label = f"below PP={pp:.5f}"
        else:
            score = 20
            label = f"above PP={pp:.5f}"
        return {"score": score, "label": label}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s09_mtf_ema_alignment(pair: str, **ctx) -> dict:
    """EMA 20/50 на M15, H1, H4 — все ли согласованы (трендовое выравнивание)."""
    try:
        score_total = 0
        labels = []
        for tf, period in [("15m", 200), ("1h", 200), ("4h", 200)]:
            bars = yahoo.latest_bars(pair, tf, period)
            if bars is None or bars.empty or len(bars) < 50:
                continue
            ema20 = ind.ema(bars["Close"], 20).dropna()
            ema50 = ind.ema(bars["Close"], 50).dropna()
            if ema20.empty or ema50.empty:
                continue
            up = ema20.iloc[-1] > ema50.iloc[-1]
            score_total += (1 if up else -1)
            labels.append(f"{tf}{'↑' if up else '↓'}")
        score = float(np.clip(score_total * 33, -100, 100))
        return {"score": score, "label": " ".join(labels) or "no tf"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s10_bb_squeeze_pos(pair: str, **ctx) -> dict:
    """Bollinger %B + bandwidth z-score. %B<0 → перепродано → BUY; >1 → SELL."""
    try:
        bars = yahoo.latest_bars(pair, "1h", 100)
        if bars is None or bars.empty or len(bars) < 30:
            return _empty_score("no data")
        bbp = ind.bollinger_pct_b(bars["Close"], 20, 2).dropna()
        if bbp.empty:
            return _empty_score("no bbp")
        cur = float(bbp.iloc[-1])
        # cur ∈ [-0.5..1.5] обычно. ниже 0 → перепродано → BUY-bias
        if cur < 0:
            score = 70
        elif cur < 0.2:
            score = 40
        elif cur > 1:
            score = -70
        elif cur > 0.8:
            score = -40
        else:
            score = (cur - 0.5) * -80  # mean-reverting bias
        return {"score": float(score), "label": f"%B={cur:.2f}"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s11_adx_like(pair: str, **ctx) -> dict:
    """Простой proxy ADX: |EMA20-EMA50|/ATR. >0.5 → сильный тренд."""
    try:
        bars = yahoo.latest_bars(pair, "1h", 100)
        if bars is None or bars.empty or len(bars) < 50:
            return _empty_score("no data")
        e20 = ind.ema(bars["Close"], 20).dropna()
        e50 = ind.ema(bars["Close"], 50).dropna()
        atr = ind.atr(bars, 14).dropna()
        if e20.empty or e50.empty or atr.empty:
            return _empty_score("no series")
        diff = float(e20.iloc[-1] - e50.iloc[-1])
        atr_v = float(atr.iloc[-1])
        if atr_v <= 0:
            return _empty_score("zero atr")
        norm = diff / atr_v  # положительный → BUY-trend
        score = float(np.clip(norm * 50, -100, 100))
        return {"score": score, "label": f"diff/ATR={norm:+.2f}"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s12_candle_pattern(pair: str, **ctx) -> dict:
    """Простой детектор pinbar / engulfing на H1."""
    try:
        bars = yahoo.latest_bars(pair, "1h", 5)
        if bars is None or bars.empty or len(bars) < 3:
            return _empty_score("no data")
        last = bars.iloc[-1]
        prev = bars.iloc[-2]
        body = abs(last["Close"] - last["Open"])
        rng = last["High"] - last["Low"]
        if rng == 0:
            return _empty_score("flat")
        upper = last["High"] - max(last["Close"], last["Open"])
        lower = min(last["Close"], last["Open"]) - last["Low"]
        # bullish pin: long lower wick
        if lower > 2 * body and lower > upper:
            return {"score": 50, "label": "bullish pin"}
        if upper > 2 * body and upper > lower:
            return {"score": -50, "label": "bearish pin"}
        # bullish engulfing
        if (prev["Close"] < prev["Open"] and last["Close"] > last["Open"] and
                last["Close"] > prev["Open"] and last["Open"] < prev["Close"]):
            return {"score": 60, "label": "bullish engulfing"}
        if (prev["Close"] > prev["Open"] and last["Close"] < last["Open"] and
                last["Close"] < prev["Open"] and last["Open"] > prev["Close"]):
            return {"score": -60, "label": "bearish engulfing"}
        # inside bar
        if last["High"] < prev["High"] and last["Low"] > prev["Low"]:
            return {"score": 0, "label": "inside bar"}
        return _empty_score("no pattern")
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s13_range_expansion(pair: str, **ctx) -> dict:
    """Сегодняшний true range / 20d median range. Расширение → продолжение тренда."""
    try:
        bars = yahoo.latest_bars(pair, "1d", 30)
        if bars is None or bars.empty or len(bars) < 5:
            return _empty_score("no data")
        rng = bars["High"] - bars["Low"]
        med = float(rng.iloc[:-1].median())
        cur = float(rng.iloc[-1])
        ratio = cur / med if med > 0 else 1.0
        if ratio < 0.7:
            return {"score": 0, "label": f"compression ×{ratio:.2f}"}
        # направление расширения
        chg = float(bars["Close"].iloc[-1] - bars["Open"].iloc[-1])
        score = float(np.clip(chg / med * 50 * ratio, -100, 100)) if med > 0 else 0
        return {"score": score, "label": f"range ×{ratio:.2f}, dir={chg:+.4f}"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s14_vp_imbalance(pair: str, **ctx) -> dict:
    """Volume Profile: объём выше POC / ниже POC. Tilt вверх → BUY."""
    try:
        bars = yahoo.latest_bars(pair, "1m", 24 * 60)
        if bars is None or bars.empty:
            return _empty_score("no data")
        vp = vp_mod.build(pair, df=bars, buckets=config.VP_BUCKETS)
        if "error" in vp:
            return _empty_score("vp err")
        poc = float(vp["poc"])
        # weight выше vs ниже POC из big_players
        bps = vp.get("big_players", [])
        wa = sum(b.get("weight_pct", 0) for b in bps if b["price"] > poc)
        wb = sum(b.get("weight_pct", 0) for b in bps if b["price"] < poc)
        total = wa + wb
        if total <= 0:
            return _empty_score("no weights")
        diff = (wa - wb) / total  # >0 → больше веса выше POC → ⇒ resistance heavy → SELL bias
        score = float(np.clip(-diff * 100, -100, 100))
        return {"score": score, "label": f"above POC={wa:.1f}% below={wb:.1f}%"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s15_news_lookahead(pair: str, **ctx) -> dict:
    """high-impact новости в ±4h → нейтрализуем сигнал; ждём до новости."""
    blackouts = ctx.get("news_blackouts") or {}
    pair_news = blackouts.get(pair) or []
    if not pair_news:
        return _empty_score("no news")
    now = _now()
    horizon = now + timedelta(hours=4)
    upcoming = [n for n in pair_news if n.get("when")
                and now <= datetime.fromisoformat(n["when"]) <= horizon]
    if not upcoming:
        return _empty_score("no news 4h")
    # есть новость впереди — приглушаем: score=0 но label важный
    return {"score": 0, "label": f"{len(upcoming)} hi-impact in 4h",
            "events": [{"when": n["when"], "title": n.get("title", "")[:50]} for n in upcoming[:3]]}


def s16_macro_tilt(pair: str, **ctx) -> dict:
    """FRED макро-tilt из agent_analyzer_fundamental_macro."""
    macro = ctx.get("fundamentals") or {}
    pair_data = (macro.get("pairs") or {}).get(pair) or {}
    tilt = pair_data.get("tilt") or pair_data.get("score") or 0
    score = float(np.clip(float(tilt) * 30, -100, 100))
    return {"score": score, "label": f"FRED tilt={tilt:+.2f}"}


def s17_cot_extreme(pair: str, **ctx) -> dict:
    """COT z-score контрапортивный: |z| > 1.5 → mean-reversion bias."""
    cot = ctx.get("cot") or {}
    pair_cot = (cot.get("pairs") or {}).get(pair) or {}
    z = float(pair_cot.get("z_score") or pair_cot.get("z") or 0)
    if abs(z) < 1.5:
        return {"score": 0, "label": f"z={z:+.2f} (no extreme)"}
    # большой положительный z (длинных слишком много) → SELL bias
    score = float(np.clip(-z * 30, -100, 100))
    return {"score": score, "label": f"z={z:+.2f} (extreme)"}


def s18_smart_money_index(pair: str, **ctx) -> dict:
    """SMI proxy: первые 30 минут сессии (открытие, движение noise) vs последний час.
    Положительный SMI → smart money покупают → BUY-bias."""
    try:
        bars = yahoo.latest_bars(pair, "30m", 50)
        if bars is None or bars.empty or len(bars) < 4:
            return _empty_score("no data")
        # за последние 24 часа: noise (первые 30m каждой сессии) vs smart (последние 60m)
        # упрощённо: first 2 bars (1h noise) vs last 2 bars (smart)
        first_dir = float(bars["Close"].iloc[1] - bars["Open"].iloc[0])
        last_dir = float(bars["Close"].iloc[-1] - bars["Open"].iloc[-2])
        # SMI = -first + last (Hedrick original)
        smi = last_dir - first_dir
        atr = ind.atr(bars, 14).dropna()
        atr_v = float(atr.iloc[-1]) if not atr.empty else 0
        if atr_v <= 0:
            return _empty_score("zero atr")
        score = float(np.clip(smi / atr_v * 50, -100, 100))
        return {"score": score, "label": f"SMI/ATR={smi/atr_v:+.2f}"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s19_yearly_position(pair: str, **ctx) -> dict:
    """Где сейчас цена в 252-дневном диапазоне (high-low). Близко к лоу → BUY-bias."""
    try:
        bars = yahoo.latest_bars(pair, "1d", 252)
        if bars is None or bars.empty or len(bars) < 30:
            return _empty_score("no data")
        h = float(bars["High"].max())
        l = float(bars["Low"].min())
        cur = float(bars["Close"].iloc[-1])
        if h <= l:
            return _empty_score("flat year")
        pos = (cur - l) / (h - l)  # 0..1
        # mean-reversion bias: pos near 0 → BUY, near 1 → SELL
        score = float(np.clip((0.5 - pos) * 200, -100, 100))
        return {"score": score, "label": f"yearly pos={pos*100:.0f}%"}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


def s20_mean_reversion_vs_momentum(pair: str, **ctx) -> dict:
    """Hurst-like: серии нескольких подряд идущих 1h-баров одного цвета → momentum.
    Чередующиеся → mean-reversion. Влияет на интерпретацию остальных сканеров."""
    try:
        bars = yahoo.latest_bars(pair, "1h", 50)
        if bars is None or bars.empty or len(bars) < 20:
            return _empty_score("no data")
        diffs = bars["Close"].diff().dropna()
        signs = np.sign(diffs.to_numpy())
        # длина max series of same sign
        max_run = 1
        cur_run = 1
        for i in range(1, len(signs)):
            if signs[i] == signs[i - 1] and signs[i] != 0:
                cur_run += 1
                max_run = max(max_run, cur_run)
            else:
                cur_run = 1
        # Если max_run >= 5 — сильный momentum в направлении последнего бара
        last_sign = signs[-1] if len(signs) > 0 else 0
        if max_run >= 5:
            score = float(last_sign * 50)
            label = f"momentum run={max_run} ({'BUY' if last_sign > 0 else 'SELL'})"
        elif max_run >= 3:
            score = float(last_sign * 25)
            label = f"weak run={max_run}"
        else:
            score = 0
            label = "mean-reverting"
        return {"score": score, "label": label}
    except Exception as e:
        return _empty_score(f"err {type(e).__name__}")


# ─────────────────────────────────────────────────────────────────────
#                              АГРЕГАЦИЯ
# ─────────────────────────────────────────────────────────────────────

ALL_SCANNERS = [
    ("currency_strength", s01_currency_strength),
    ("correlation_divergence", s02_correlation_divergence),
    ("triangular_arb", s03_triangular_arb),
    ("volatility_regime", s04_volatility_regime),
    ("session_momentum", s05_session_momentum),
    ("hour_of_day_bias", s06_hour_of_day_bias),
    ("dow_bias", s07_dow_bias),
    ("pivot_distance", s08_pivot_distance),
    ("mtf_ema_alignment", s09_mtf_ema_alignment),
    ("bb_squeeze_pos", s10_bb_squeeze_pos),
    ("adx_like", s11_adx_like),
    ("candle_pattern", s12_candle_pattern),
    ("range_expansion", s13_range_expansion),
    ("vp_imbalance", s14_vp_imbalance),
    ("news_lookahead", s15_news_lookahead),
    ("macro_tilt", s16_macro_tilt),
    ("cot_extreme", s17_cot_extreme),
    ("smart_money_index", s18_smart_money_index),
    ("yearly_position", s19_yearly_position),
    ("mean_rev_vs_momentum", s20_mean_reversion_vs_momentum),
]


def _build_currency_strength(pairs: list[str]) -> dict[str, float]:
    """24h % изменения каждой валюты, агрегировано через все пары где она встречается."""
    per_ccy: dict[str, list[float]] = {}
    for pair in pairs:
        try:
            bars = yahoo.latest_bars(pair, "1h", 24)
            if bars is None or bars.empty or len(bars) < 6:
                continue
            chg = float((bars["Close"].iloc[-1] - bars["Close"].iloc[0]) /
                        bars["Close"].iloc[0])
            base, quote = _decompose(pair)
            per_ccy.setdefault(base, []).append(chg)
            per_ccy.setdefault(quote, []).append(-chg)
        except Exception:
            continue
    return {ccy: float(np.mean(vals)) * 100 for ccy, vals in per_ccy.items() if vals}


def cycle_once() -> dict:
    """Главный tick: бежит все 20 сканеров по всем 28 парам. ~30-50 секунд."""
    t0 = time.time()
    pairs = list(config.PAIRS)

    # Загружаем общий контекст один раз
    closed = []
    try:
        cf = config.STATE_DIR / "closed_trades.json"
        if cf.exists():
            closed = json.loads(cf.read_text())
    except Exception:
        pass

    fundamentals = {}
    try:
        ff = config.STATE_DIR / "agent_analyzer_fundamental_macro.json"
        if ff.exists():
            fundamentals = json.loads(ff.read_text())
    except Exception:
        pass

    cot = {}
    try:
        cf = config.STATE_DIR / "agent_analyzer_cot_positioning.json"
        if cf.exists():
            cot = json.loads(cf.read_text())
    except Exception:
        pass

    news_blackouts = {}
    try:
        nf = config.STATE_DIR / "news_blackouts.json"
        if nf.exists():
            news_blackouts = json.loads(nf.read_text())
    except Exception:
        pass

    currency_strength = _build_currency_strength(pairs)

    ctx = {
        "closed_trades": closed,
        "fundamentals": fundamentals,
        "cot": cot,
        "news_blackouts": news_blackouts,
        "currency_strength": currency_strength,
    }

    out_pairs: dict[str, dict] = {}
    for pair in pairs:
        scanner_results = {}
        total = 0.0
        n_pass = 0
        for name, fn in ALL_SCANNERS:
            try:
                r = fn(pair, **ctx)
            except Exception as e:
                r = {"score": 0.0, "label": f"crash {type(e).__name__}"}
            scanner_results[name] = r
            score = float(r.get("score") or 0.0)
            total += score
            if abs(score) >= SCANNER_PASS_THRESHOLD:
                n_pass += 1
        # нормализация: avg score через 20 сканеров
        n = len(ALL_SCANNERS)
        avg = total / n
        # bias: BUY если > NEUTRAL_THRESHOLD, SELL если < -threshold
        if avg > NEUTRAL_THRESHOLD:
            direction = "BUY"
        elif avg < -NEUTRAL_THRESHOLD:
            direction = "SELL"
        else:
            direction = "NEUTRAL"
        out_pairs[pair] = {
            "overall_score": round(avg, 2),
            "direction": direction,
            "scanners_passing": n_pass,
            "scanners_total": n,
            "scanners": scanner_results,
        }

    elapsed = time.time() - t0
    payload = {
        "as_of": _now().isoformat(),
        "elapsed_sec": round(elapsed, 1),
        "scanners": [name for name, _ in ALL_SCANNERS],
        "scanner_count": len(ALL_SCANNERS),
        "neutral_threshold": NEUTRAL_THRESHOLD,
        "pass_threshold": SCANNER_PASS_THRESHOLD,
        "pairs": out_pairs,
    }
    RADAR_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log.info(f"radar cycle: {len(pairs)} pairs × {len(ALL_SCANNERS)} scanners "
             f"in {elapsed:.1f}s")
    return payload


def main_loop() -> None:
    log.info("market_radar started; interval=%ds", RADAR_INTERVAL_SEC)
    while True:
        _heartbeat()
        try:
            cycle_once()
        except Exception:
            log.exception("radar cycle crashed")
        time.sleep(RADAR_INTERVAL_SEC)


if __name__ == "__main__":
    main_loop()
