"""strategies — каталог стратегий-вариантов для strategy_search.

Каждый вариант это Strategy: набор фильтров входа + правила формирования
score + recommended expiry. Бэктестер прогоняет одну и ту же 30-дневную
историю под каждой стратегией и считает WR. strategy_search выбирает
лучшую стратегию для каждой пары.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable, Optional

import pandas as pd

from . import config, indicators


SessionFilter = Optional[tuple[int, int]]  # (start_hour_utc, end_hour_utc) или None для всех часов


@dataclass
class Strategy:
    id: str
    label: str
    # фильтры
    min_abs_score: int = 8        # минимум |score| для открытия
    min_probability: float = 0.70  # минимум probability
    session_utc: SessionFilter = None  # фильтр по часу UTC
    require_full_mtf_alignment: bool = False  # блок A в одну сторону на ВСЕХ 3 TF
    fade_extreme_rsi: bool = False  # на RSI<25/RSI>75 открывать ОБРАТНО
    fixed_expiry_h: Optional[int] = None  # принудительная экспирация (None = recommended)
    contrarian: bool = False  # перевернуть направление сделки (BUY <-> SELL)
    require_strong_volume: bool = False  # 1H volume > avg(volume)*1.2
    # веса блоков (1.0 = базовый)
    weight_block_a: float = 1.0  # старший TF
    weight_block_b: float = 1.0  # RSI
    weight_block_c: float = 1.0  # Bollinger
    weight_block_d: float = 1.0  # Momentum
    weight_block_e: float = 1.0  # CEI/OFI
    weight_block_f: float = 1.0  # VWAP
    weight_block_g: float = 1.0  # BBP
    weight_block_h: float = 1.0  # MTF consensus

    def __repr__(self) -> str:
        return f"<Strategy {self.id}>"


def _compute_score(strategy: Strategy, ind_4h: dict, ind_1h: dict, ind_15m: dict) -> int:
    score = 0.0
    # BLOCK A — структура старшего TF
    if ind_4h["close"] > ind_4h["ema50"] > ind_4h["ema200"]:
        score += 3 * strategy.weight_block_a
    elif ind_4h["close"] < ind_4h["ema50"] < ind_4h["ema200"]:
        score -= 3 * strategy.weight_block_a
    elif ind_4h["close"] > ind_4h["ema50"]:
        score += 1 * strategy.weight_block_a
    elif ind_4h["close"] < ind_4h["ema50"]:
        score -= 1 * strategy.weight_block_a

    if ind_1h["close"] > ind_1h["ema20"] > ind_1h["ema50"]:
        score += 2 * strategy.weight_block_a
    elif ind_1h["close"] < ind_1h["ema20"] < ind_1h["ema50"]:
        score -= 2 * strategy.weight_block_a

    if ind_15m["close"] > ind_15m["ema20"]:
        score += 1 * strategy.weight_block_a
    else:
        score -= 1 * strategy.weight_block_a

    # BLOCK B — RSI
    rsi = ind_1h["rsi14"]
    if 50 < rsi < 70:
        score += 2 * strategy.weight_block_b
    elif 30 < rsi < 50:
        score -= 2 * strategy.weight_block_b
    elif rsi >= 70:
        score -= 2 * strategy.weight_block_b
    elif rsi <= 30:
        score += 2 * strategy.weight_block_b

    # BLOCK C — Bollinger %B
    bb = ind_1h["bb_pct"]
    if bb > 0.95:
        score -= 1 * strategy.weight_block_c
    elif bb < 0.05:
        score += 1 * strategy.weight_block_c
    elif 0.5 < bb < 0.85:
        score += 1 * strategy.weight_block_c
    elif 0.15 < bb < 0.5:
        score -= 1 * strategy.weight_block_c

    # BLOCK D — Momentum
    if ind_1h["mom5"] > 0.1:
        score += 2 * strategy.weight_block_d
    elif ind_1h["mom5"] < -0.1:
        score -= 2 * strategy.weight_block_d

    # BLOCK E — CEI/OFI
    if ind_1h["cei10"] > 60 and ind_1h["ofi10"] > 0.3:
        score += 2 * strategy.weight_block_e
    elif ind_1h["cei10"] > 60 and ind_1h["ofi10"] < -0.3:
        score -= 2 * strategy.weight_block_e

    # BLOCK F — VWAP
    if ind_1h["close"] > ind_1h["vwap"] * 1.001:
        score += 1 * strategy.weight_block_f
    elif ind_1h["close"] < ind_1h["vwap"] * 0.999:
        score -= 1 * strategy.weight_block_f

    # BLOCK G — BBP regime
    if ind_1h["bbp"] > 0:
        score += 1 * strategy.weight_block_g
    elif ind_1h["bbp"] < 0:
        score -= 1 * strategy.weight_block_g

    # BLOCK H — multi-TF consensus
    bull_count = (
        int(ind_4h["close"] > ind_4h["ema50"])
        + int(ind_1h["close"] > ind_1h["ema20"])
        + int(ind_15m["close"] > ind_15m["ema20"])
    )
    if bull_count == 3:
        score += 3 * strategy.weight_block_h
    elif bull_count == 0:
        score -= 3 * strategy.weight_block_h

    return int(round(score))


def evaluate(strategy: Strategy,
             ts: pd.Timestamp,
             ind_4h: dict, ind_1h: dict, ind_15m: dict) -> Optional[tuple[str, int, int, float]]:
    """
    Применяет стратегию к одному тик-снимку. Возвращает либо None (не открываем),
    либо (side, score, expiry_h, probability_pct).
    """
    # session filter
    if strategy.session_utc is not None:
        h = ts.hour
        s, e = strategy.session_utc
        if s <= e:
            if not (s <= h < e):
                return None
        else:
            # окно через полночь (NY 13-22 — нормально, но Asia 22-08)
            if not (h >= s or h < e):
                return None

    # mandatory full multi-TF alignment
    if strategy.require_full_mtf_alignment:
        bull_count = (
            int(ind_4h["close"] > ind_4h["ema50"])
            + int(ind_1h["close"] > ind_1h["ema20"])
            + int(ind_15m["close"] > ind_15m["ema20"])
        )
        if bull_count not in (0, 3):
            return None

    # volume filter
    if strategy.require_strong_volume:
        vol = ind_1h.get("volume_now", 0)
        avg_vol = ind_1h.get("volume_avg20", 0)
        if avg_vol <= 0 or vol < avg_vol * 1.2:
            return None

    score = _compute_score(strategy, ind_4h, ind_1h, ind_15m)

    # fade RSI extremes — переворот score
    if strategy.fade_extreme_rsi:
        rsi = ind_1h["rsi14"]
        if rsi <= 25 or rsi >= 75:
            score = -score

    if strategy.contrarian:
        score = -score

    if score == 0:
        return None

    if abs(score) < strategy.min_abs_score:
        return None

    import math
    p = 1.0 / (1.0 + math.exp(-(score / 44) * 4.0))
    p = max(0.50, min(config.MAX_PROBABILITY, p))
    if p < strategy.min_probability:
        return None

    side = "BUY" if score > 0 else "SELL"

    if strategy.fixed_expiry_h is not None:
        rec_h = strategy.fixed_expiry_h
    else:
        abs_norm = min(1.0, abs(score) / 20.0)
        rec_h = int(round(config.MIN_EXPIRY_HOURS + abs_norm * (config.MAX_EXPIRY_HOURS - config.MIN_EXPIRY_HOURS)))
        rec_h = max(config.MIN_EXPIRY_HOURS, min(config.MAX_EXPIRY_HOURS, rec_h))

    return side, score, rec_h, p


# ---------- библиотека вариантов ----------

VARIANTS: list[Strategy] = [
    # baseline
    Strategy("v01_baseline", "baseline (текущая)"),

    # пороги уверенности
    Strategy("v02_score12", "|score|>=12", min_abs_score=12),
    Strategy("v03_score16", "|score|>=16", min_abs_score=16),
    Strategy("v04_prob75", "prob>=75%", min_probability=0.75),
    Strategy("v05_prob80", "prob>=80%", min_probability=0.80),
    Strategy("v06_prob85", "prob>=85%", min_probability=0.85),
    Strategy("v07_high_conv", "|score|>=12 + prob>=75", min_abs_score=12, min_probability=0.75),
    Strategy("v08_max_conv", "|score|>=16 + prob>=80", min_abs_score=16, min_probability=0.80),

    # экспирация
    Strategy("v09_exp1h", "expiry=1ч", fixed_expiry_h=1),
    Strategy("v10_exp2h", "expiry=2ч", fixed_expiry_h=2),
    Strategy("v11_exp4h", "expiry=4ч", fixed_expiry_h=4),

    # сессии (UTC)
    Strategy("v12_london", "London 07-15 UTC", session_utc=(7, 15)),
    Strategy("v13_ny", "NY 13-22 UTC", session_utc=(13, 22)),
    Strategy("v14_asia", "Asia 00-08 UTC", session_utc=(0, 8)),
    Strategy("v15_overlap", "London/NY overlap 13-15 UTC", session_utc=(13, 15)),

    # MTF alignment
    Strategy("v16_full_mtf", "full MTF alignment", require_full_mtf_alignment=True),
    Strategy("v17_full_mtf_high", "full MTF + |score|>=12", require_full_mtf_alignment=True, min_abs_score=12),

    # contrarian / fade
    Strategy("v18_fade_rsi", "fade RSI extremes", fade_extreme_rsi=True),
    Strategy("v19_contrarian", "contrarian (флип всех)", contrarian=True),
    Strategy("v20_contra_high", "contrarian + |score|>=16", contrarian=True, min_abs_score=16),

    # веса блоков
    Strategy("v21_emph_mtf", "emphasis: MTF (block A x2 + H x2)",
             weight_block_a=2.0, weight_block_h=2.0),
    Strategy("v22_emph_momentum", "emphasis: Momentum (block D x2 + E x2)",
             weight_block_d=2.0, weight_block_e=2.0),
    Strategy("v23_emph_meanrev", "emphasis: mean-reversion (B x2 + C x2)",
             weight_block_b=2.0, weight_block_c=2.0),
    Strategy("v24_only_mtf", "only structure (A,H weight x3, остальное x0)",
             weight_block_a=3.0, weight_block_h=3.0,
             weight_block_b=0.0, weight_block_c=0.0, weight_block_d=0.0,
             weight_block_e=0.0, weight_block_f=0.0, weight_block_g=0.0),

    # комбо «как у профи»
    Strategy("v25_pro_trend",
             "PRO: London/NY + full MTF + |score|>=12 + exp=2ч",
             session_utc=(7, 22),
             require_full_mtf_alignment=True,
             min_abs_score=12,
             fixed_expiry_h=2),
    Strategy("v26_pro_quick",
             "PRO: London/NY + |score|>=16 + exp=1ч",
             session_utc=(7, 22),
             min_abs_score=16,
             fixed_expiry_h=1),
    Strategy("v27_pro_slow",
             "PRO: London/NY + |score|>=12 + exp=4ч + prob>=75",
             session_utc=(7, 22),
             min_abs_score=12,
             min_probability=0.75,
             fixed_expiry_h=4),
    Strategy("v28_pro_fade",
             "PRO: fade RSI + London/NY + |score|>=12 + exp=2ч",
             session_utc=(7, 22),
             fade_extreme_rsi=True,
             min_abs_score=12,
             fixed_expiry_h=2),
    Strategy("v29_pro_contra",
             "PRO: contrarian + |score|>=16 + Asia + exp=4ч",
             contrarian=True,
             min_abs_score=16,
             session_utc=(0, 8),
             fixed_expiry_h=4),
    Strategy("v30_pro_mtf_strict",
             "PRO: full MTF + |score|>=16 + London/NY + exp=2ч + prob>=80",
             require_full_mtf_alignment=True,
             min_abs_score=16,
             session_utc=(7, 22),
             fixed_expiry_h=2,
             min_probability=0.80),

    # ─── ДОПОЛНИТЕЛЬНЫЕ варианты для более глубокого поиска ───
    # Цель: чтобы strategy_search per-session (Asia / London / Overlap / NY)
    # имел больше "кандидатов" найти ≥70% WR в КАЖДОЙ сессии для КАЖДОЙ пары.
    # Эти варианты НЕ имеют session_utc — strategy_search per-session навешивает
    # сессию извне. Любые варианты с session_utc применяются дополнительно.

    # тонкая шкала score
    Strategy("v31_score10", "|score|>=10", min_abs_score=10),
    Strategy("v32_score14", "|score|>=14", min_abs_score=14),
    Strategy("v33_score18", "|score|>=18", min_abs_score=18),
    Strategy("v34_score20", "|score|>=20", min_abs_score=20),

    # высокая уверенность в комбо
    Strategy("v35_score10_prob78", "|score|>=10 + prob>=78%",
             min_abs_score=10, min_probability=0.78),
    Strategy("v36_score14_prob82", "|score|>=14 + prob>=82%",
             min_abs_score=14, min_probability=0.82),
    Strategy("v37_score18_prob85", "|score|>=18 + prob>=85%",
             min_abs_score=18, min_probability=0.85),

    # full MTF + разные пороги
    Strategy("v38_full_mtf_score14", "full MTF + |score|>=14",
             require_full_mtf_alignment=True, min_abs_score=14),
    Strategy("v39_full_mtf_score18", "full MTF + |score|>=18",
             require_full_mtf_alignment=True, min_abs_score=18),
    Strategy("v40_full_mtf_prob80", "full MTF + prob>=80%",
             require_full_mtf_alignment=True, min_probability=0.80),
    Strategy("v41_full_mtf_prob85", "full MTF + prob>=85%",
             require_full_mtf_alignment=True, min_probability=0.85),

    # full MTF + emphasis комбо
    Strategy("v42_full_mtf_trend",
             "full MTF + structure x2 + |score|>=14",
             require_full_mtf_alignment=True,
             weight_block_a=2.0, weight_block_h=2.0,
             min_abs_score=14),
    Strategy("v43_full_mtf_momentum",
             "full MTF + momentum x2 + |score|>=14",
             require_full_mtf_alignment=True,
             weight_block_d=2.0, weight_block_e=2.0,
             min_abs_score=14),

    # пары экспирация × score
    Strategy("v44_exp1h_score14", "exp=1ч + |score|>=14",
             fixed_expiry_h=1, min_abs_score=14),
    Strategy("v45_exp2h_score14", "exp=2ч + |score|>=14",
             fixed_expiry_h=2, min_abs_score=14),
    Strategy("v46_exp3h_score14", "exp=3ч + |score|>=14",
             fixed_expiry_h=3, min_abs_score=14),
    Strategy("v47_exp4h_score14", "exp=4ч + |score|>=14",
             fixed_expiry_h=4, min_abs_score=14),
    Strategy("v48_exp4h_score18_prob80",
             "exp=4ч + |score|>=18 + prob>=80%",
             fixed_expiry_h=4, min_abs_score=18, min_probability=0.80),

    # contrarian + высокая уверенность
    Strategy("v49_contra_score14", "contrarian + |score|>=14",
             contrarian=True, min_abs_score=14),
    Strategy("v50_contra_score18_prob80",
             "contrarian + |score|>=18 + prob>=80%",
             contrarian=True, min_abs_score=18, min_probability=0.80),

    # mean-reversion emphasis × score
    Strategy("v51_meanrev_score12",
             "mean-reversion (B,C x2) + |score|>=12",
             weight_block_b=2.0, weight_block_c=2.0, min_abs_score=12),
    Strategy("v52_meanrev_score16",
             "mean-reversion (B,C x2) + |score|>=16",
             weight_block_b=2.0, weight_block_c=2.0, min_abs_score=16),

    # only-structure × score
    Strategy("v53_only_struct_score10",
             "only structure (A,H x3) + |score|>=10",
             weight_block_a=3.0, weight_block_h=3.0,
             weight_block_b=0.0, weight_block_c=0.0, weight_block_d=0.0,
             weight_block_e=0.0, weight_block_f=0.0, weight_block_g=0.0,
             min_abs_score=10),
    Strategy("v54_only_struct_score14",
             "only structure (A,H x3) + |score|>=14",
             weight_block_a=3.0, weight_block_h=3.0,
             weight_block_b=0.0, weight_block_c=0.0, weight_block_d=0.0,
             weight_block_e=0.0, weight_block_f=0.0, weight_block_g=0.0,
             min_abs_score=14),

    # max conviction
    Strategy("v55_max_strict",
             "MAX: full MTF + |score|>=20 + prob>=85%",
             require_full_mtf_alignment=True,
             min_abs_score=20, min_probability=0.85),
    Strategy("v56_extreme",
             "EXTREME: full MTF + |score|>=24 + prob>=88%",
             require_full_mtf_alignment=True,
             min_abs_score=24, min_probability=0.88),

    # fade RSI extremes комбо
    Strategy("v57_fade_score14", "fade RSI + |score|>=14",
             fade_extreme_rsi=True, min_abs_score=14),
    Strategy("v58_fade_full_mtf",
             "fade RSI + full MTF + |score|>=12",
             fade_extreme_rsi=True, require_full_mtf_alignment=True,
             min_abs_score=12),

    # volume + score (требует наличие volume в данных Yahoo, может быть no-op)
    Strategy("v59_vol_score14", "strong vol + |score|>=14",
             require_strong_volume=True, min_abs_score=14),
    Strategy("v60_vol_full_mtf",
             "strong vol + full MTF + |score|>=12",
             require_strong_volume=True, require_full_mtf_alignment=True,
             min_abs_score=12),
]


def variants_by_id() -> dict[str, Strategy]:
    return {s.id: s for s in VARIANTS}


# ─── Канонические торговые сессии (UTC) для per-session strategy_search ───
# Не пересекаются: каждый час UTC попадает ровно в одну сессию (или Off).
# strategy_search прогоняет ВСЕ VARIANTS отдельно по каждой сессии и выбирает
# лучшую стратегию для каждой пары × каждой сессии.
SESSION_WINDOWS: dict[str, tuple[int, int]] = {
    "Asia":    (0,  7),   # 00:00–06:59 UTC (Tokyo / Sydney активность)
    "London":  (7,  13),  # 07:00–12:59 UTC (London open до открытия NY)
    "Overlap": (13, 17),  # 13:00–16:59 UTC (London/NY overlap, самая высокая ликвидность)
    "NY":      (17, 22),  # 17:00–21:59 UTC (NY после закрытия Лондона)
}


def detect_session(hour_utc: int) -> str | None:
    """Какая сессия соответствует данному часу UTC. None если час 22:00–23:59."""
    for name, (s, e) in SESSION_WINDOWS.items():
        if s <= hour_utc < e:
            return name
    return None  # off-hours (22-23 UTC) — не торгуем
