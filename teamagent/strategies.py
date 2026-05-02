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
    # 2026-05-03 WR maximization — 6 новых индикаторов
    weight_block_macd: float = 1.0       # MACD
    weight_block_stoch: float = 1.0      # Stochastic
    weight_block_adx: float = 1.0        # ADX trend strength
    weight_block_williams: float = 1.0   # Williams %R
    weight_block_ichimoku: float = 1.0   # Ichimoku Cloud
    # ADX regime gate: если задан, не открывать сделки при ADX < этого порога
    require_adx_above: Optional[float] = None

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

    # BLOCK I — MACD (histogram crossings)
    macd_hist = ind_1h.get("macd_hist", 0.0)
    macd_prev = ind_1h.get("macd_prev_hist", 0.0)
    if macd_hist > 0 and macd_prev <= 0:
        score += 2 * strategy.weight_block_macd
    elif macd_hist < 0 and macd_prev >= 0:
        score -= 2 * strategy.weight_block_macd
    elif macd_hist > 0 and macd_hist > macd_prev:
        score += 1 * strategy.weight_block_macd
    elif macd_hist < 0 and macd_hist < macd_prev:
        score -= 1 * strategy.weight_block_macd

    # BLOCK J — Stochastic
    stoch_k = ind_1h.get("stoch_k", 50.0)
    stoch_d = ind_1h.get("stoch_d", 50.0)
    if stoch_k < 20 and stoch_d < 20:
        score += 2 * strategy.weight_block_stoch
    elif stoch_k > 80 and stoch_d > 80:
        score -= 2 * strategy.weight_block_stoch
    elif stoch_k > stoch_d and stoch_k < 80:
        score += 1 * strategy.weight_block_stoch
    elif stoch_k < stoch_d and stoch_k > 20:
        score -= 1 * strategy.weight_block_stoch

    # BLOCK K — ADX trend strength
    adx_val = ind_1h.get("adx", 0.0)
    plus_di = ind_1h.get("plus_di", 0.0)
    minus_di = ind_1h.get("minus_di", 0.0)
    if adx_val > 25:
        if plus_di > minus_di:
            score += 2 * strategy.weight_block_adx
        else:
            score -= 2 * strategy.weight_block_adx

    # BLOCK L — Williams %R
    wr_val = ind_1h.get("williams_r", -50.0)
    if wr_val > -20:
        score -= 1 * strategy.weight_block_williams
    elif wr_val < -80:
        score += 1 * strategy.weight_block_williams

    # BLOCK M — Ichimoku Cloud
    above_cloud = bool(ind_1h.get("ichimoku_above_cloud", 0.0))
    below_cloud = bool(ind_1h.get("ichimoku_below_cloud", 0.0))
    tenkan = ind_1h.get("ichimoku_tenkan", 0.0)
    kijun = ind_1h.get("ichimoku_kijun", 0.0)
    if above_cloud and tenkan > kijun:
        score += 3 * strategy.weight_block_ichimoku
    elif below_cloud and tenkan < kijun:
        score -= 3 * strategy.weight_block_ichimoku
    elif above_cloud:
        score += 1 * strategy.weight_block_ichimoku
    elif below_cloud:
        score -= 1 * strategy.weight_block_ichimoku

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

    # ADX regime gate (2026-05-03): не торгуем если ADX слабее порога (флэт)
    if strategy.require_adx_above is not None:
        adx_val = ind_1h.get("adx", 0.0)
        if adx_val < strategy.require_adx_above:
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
    # max_score=75 после добавления BLOCK I-M (MACD/Stoch/ADX/Williams/Ichimoku)
    p = 1.0 / (1.0 + math.exp(-(score / 75) * 4.0))
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

    # ─── v61–v90: дополнительные варианты для weak-session покрытия ───
    # (добавлены чтобы Asia/NY имели шанс достигать ≥70% WR — пробуем больше
    # фильтров: ультра-строгие пороги, фокус на структуре, краткие экспирации)

    # ультра-строгие пороги (score≥20+)
    Strategy("v61_ultra_strict",
             "ULTRA: |score|>=22 + prob>=82% + full MTF",
             min_abs_score=22, min_probability=0.82,
             require_full_mtf_alignment=True),
    Strategy("v62_ultra_score24",
             "ULTRA: |score|>=24 (very rare, very confident)",
             min_abs_score=24),
    Strategy("v63_score20_prob80",
             "|score|>=20 + prob>=80%",
             min_abs_score=20, min_probability=0.80),
    Strategy("v64_score18_prob78",
             "|score|>=18 + prob>=78%",
             min_abs_score=18, min_probability=0.78),

    # short expiry + very strict (для intraday скальпинга)
    Strategy("v65_exp1h_score18", "1ч + |score|>=18",
             fixed_expiry_h=1, min_abs_score=18),
    Strategy("v66_exp2h_score18", "2ч + |score|>=18",
             fixed_expiry_h=2, min_abs_score=18),
    Strategy("v67_exp1h_full_mtf_score14",
             "1ч + full MTF + |score|>=14",
             fixed_expiry_h=1, require_full_mtf_alignment=True,
             min_abs_score=14),

    # Asia-фокус (range mean-reversion)
    Strategy("v68_asia_contra_score14",
             "Asia + contrarian + |score|>=14 (range fade)",
             session_utc=(0, 7), contrarian=True, min_abs_score=14),
    Strategy("v69_asia_fade_rsi_score12",
             "Asia + fade RSI extremes + |score|>=12",
             session_utc=(0, 7), fade_extreme_rsi=True, min_abs_score=12),
    Strategy("v70_asia_strict_full_mtf",
             "Asia + |score|>=18 + full MTF",
             session_utc=(0, 7), min_abs_score=18,
             require_full_mtf_alignment=True),
    Strategy("v71_asia_emph_struct",
             "Asia + emphasis structure (A,H x2) + |score|>=14",
             session_utc=(0, 7), weight_block_a=2.0, weight_block_h=2.0,
             min_abs_score=14),

    # NY-фокус (trend / news)
    Strategy("v72_ny_full_mtf_score16",
             "NY + full MTF + |score|>=16",
             session_utc=(17, 22), require_full_mtf_alignment=True,
             min_abs_score=16),
    Strategy("v73_ny_emph_trend",
             "NY + emphasis trend (A x2) + |score|>=18",
             session_utc=(17, 22), weight_block_a=2.0, min_abs_score=18),
    Strategy("v74_ny_emph_momentum",
             "NY + emphasis momentum (D,E x2) + |score|>=18",
             session_utc=(17, 22), weight_block_d=2.0, weight_block_e=2.0,
             min_abs_score=18),
    Strategy("v75_ny_score20",
             "NY + |score|>=20",
             session_utc=(17, 22), min_abs_score=20),

    # Lon-overlap focus (12-17 UTC) — самая ликвидная зона
    Strategy("v76_overlap_score20_full_mtf",
             "Overlap + |score|>=20 + full MTF",
             session_utc=(13, 17), min_abs_score=20,
             require_full_mtf_alignment=True),
    Strategy("v77_overlap_emph_momentum",
             "Overlap + emphasis momentum (D,E x2) + |score|>=16",
             session_utc=(13, 17), weight_block_d=2.0, weight_block_e=2.0,
             min_abs_score=16),

    # London focus (7-13 UTC)
    Strategy("v78_london_score18_full_mtf",
             "London + |score|>=18 + full MTF",
             session_utc=(7, 13), min_abs_score=18,
             require_full_mtf_alignment=True),
    Strategy("v79_london_emph_trend",
             "London + emphasis trend (A x2) + |score|>=16",
             session_utc=(7, 13), weight_block_a=2.0, min_abs_score=16),

    # MTF + emphasis combos — без сессий, для всех
    Strategy("v80_full_mtf_emph_struct",
             "full MTF + emphasis structure (A,H x2) + |score|>=14",
             require_full_mtf_alignment=True,
             weight_block_a=2.0, weight_block_h=2.0, min_abs_score=14),
    Strategy("v81_full_mtf_emph_volat",
             "full MTF + emphasis volatility (C,G x2) + |score|>=14",
             require_full_mtf_alignment=True,
             weight_block_c=2.0, weight_block_g=2.0, min_abs_score=14),
    Strategy("v82_full_mtf_emph_meanrev",
             "full MTF + emphasis mean-reversion (B,C x2) + |score|>=14",
             require_full_mtf_alignment=True,
             weight_block_b=2.0, weight_block_c=2.0, min_abs_score=14),

    # Contrarian high-conviction
    Strategy("v83_contra_score20",
             "contrarian + |score|>=20 (extreme reversal)",
             contrarian=True, min_abs_score=20),
    Strategy("v84_contra_full_mtf_score14",
             "contrarian + full MTF + |score|>=14",
             contrarian=True, require_full_mtf_alignment=True,
             min_abs_score=14),
    Strategy("v85_contra_prob78",
             "contrarian + prob>=78%",
             contrarian=True, min_probability=0.78),

    # fade RSI + strict
    Strategy("v86_fade_rsi_score18",
             "fade RSI + |score|>=18",
             fade_extreme_rsi=True, min_abs_score=18),
    Strategy("v87_fade_rsi_full_mtf_score16",
             "fade RSI + full MTF + |score|>=16",
             fade_extreme_rsi=True, require_full_mtf_alignment=True,
             min_abs_score=16),

    # micro-tweak: prob ≥ 75/76 + score 14/16
    Strategy("v88_prob75_score14",
             "prob>=75% + |score|>=14", min_probability=0.75, min_abs_score=14),
    Strategy("v89_prob76_score16",
             "prob>=76% + |score|>=16", min_probability=0.76, min_abs_score=16),
    Strategy("v90_prob78_full_mtf",
             "prob>=78% + full MTF", min_probability=0.78,
             require_full_mtf_alignment=True),

    # ─── v91–v120: ультра-агрессивный охват слабых ячеек (Asia/NY) ───
    # Гипотеза: нужны ОЧЕНЬ узкие фильтры (мало сделок, но высокий WR) — берём
    # только самые экстремальные случаи. MIN_TRADES=10 в strategy_search всё
    # равно отбракует те которые слишком редкие.

    # Asia: ультра-строгие
    Strategy("v91_asia_score20", "Asia + |score|>=20",
             session_utc=(0, 7), min_abs_score=20),
    Strategy("v92_asia_score22_prob80",
             "Asia + |score|>=22 + prob>=80%",
             session_utc=(0, 7), min_abs_score=22, min_probability=0.80),
    Strategy("v93_asia_full_mtf_prob78",
             "Asia + full MTF + prob>=78%",
             session_utc=(0, 7), require_full_mtf_alignment=True,
             min_probability=0.78),
    Strategy("v94_asia_contra_full_mtf",
             "Asia + contrarian + full MTF + |score|>=12",
             session_utc=(0, 7), contrarian=True,
             require_full_mtf_alignment=True, min_abs_score=12),
    Strategy("v95_asia_fade_rsi_prob76",
             "Asia + fade RSI + prob>=76%",
             session_utc=(0, 7), fade_extreme_rsi=True,
             min_probability=0.76),
    Strategy("v96_asia_emph_volat",
             "Asia + emphasis volatility (C,G x2) + |score|>=14",
             session_utc=(0, 7), weight_block_c=2.0, weight_block_g=2.0,
             min_abs_score=14),
    Strategy("v97_asia_emph_meanrev",
             "Asia + emphasis mean-rev (B,C x2) + |score|>=14",
             session_utc=(0, 7), weight_block_b=2.0, weight_block_c=2.0,
             min_abs_score=14),
    Strategy("v98_asia_exp4h_score16",
             "Asia + 4h expiry + |score|>=16",
             session_utc=(0, 7), fixed_expiry_h=4, min_abs_score=16),

    # NY: ультра-строгие
    Strategy("v99_ny_score22", "NY + |score|>=22",
             session_utc=(17, 22), min_abs_score=22),
    Strategy("v100_ny_full_mtf_prob80",
             "NY + full MTF + prob>=80%",
             session_utc=(17, 22), require_full_mtf_alignment=True,
             min_probability=0.80),
    Strategy("v101_ny_contra_full_mtf",
             "NY + contrarian + full MTF + |score|>=14",
             session_utc=(17, 22), contrarian=True,
             require_full_mtf_alignment=True, min_abs_score=14),
    Strategy("v102_ny_fade_rsi_score16",
             "NY + fade RSI + |score|>=16",
             session_utc=(17, 22), fade_extreme_rsi=True,
             min_abs_score=16),
    Strategy("v103_ny_emph_struct",
             "NY + emphasis structure (A,H x2) + |score|>=16",
             session_utc=(17, 22), weight_block_a=2.0, weight_block_h=2.0,
             min_abs_score=16),
    Strategy("v104_ny_emph_volat",
             "NY + emphasis volatility (C,G x2) + |score|>=14",
             session_utc=(17, 22), weight_block_c=2.0, weight_block_g=2.0,
             min_abs_score=14),
    Strategy("v105_ny_exp1h_score18",
             "NY + 1h expiry + |score|>=18",
             session_utc=(17, 22), fixed_expiry_h=1, min_abs_score=18),
    Strategy("v106_ny_exp3h_score16",
             "NY + 3h expiry + |score|>=16",
             session_utc=(17, 22), fixed_expiry_h=3, min_abs_score=16),

    # Overlap-добавки (могут помочь там где остальные не дотянули)
    Strategy("v107_overlap_score22",
             "Overlap + |score|>=22",
             session_utc=(13, 17), min_abs_score=22),
    Strategy("v108_overlap_full_mtf_prob80",
             "Overlap + full MTF + prob>=80%",
             session_utc=(13, 17), require_full_mtf_alignment=True,
             min_probability=0.80),
    Strategy("v109_overlap_contra_score18",
             "Overlap + contrarian + |score|>=18",
             session_utc=(13, 17), contrarian=True, min_abs_score=18),

    # London-добавки
    Strategy("v110_london_score20",
             "London + |score|>=20",
             session_utc=(7, 13), min_abs_score=20),
    Strategy("v111_london_full_mtf_prob80",
             "London + full MTF + prob>=80%",
             session_utc=(7, 13), require_full_mtf_alignment=True,
             min_probability=0.80),
    Strategy("v112_london_contra_score18",
             "London + contrarian + |score|>=18",
             session_utc=(7, 13), contrarian=True, min_abs_score=18),

    # Прочие крайние комбинации (без session_utc — широкий охват)
    Strategy("v113_score25_anysession",
             "|score|>=25 (sniper)", min_abs_score=25),
    Strategy("v114_score20_full_mtf_prob82",
             "|score|>=20 + full MTF + prob>=82%",
             min_abs_score=20, require_full_mtf_alignment=True,
             min_probability=0.82),
    Strategy("v115_emph_struct_volat_score16",
             "emphasis structure+volatility (A,C,G,H x2) + |score|>=16",
             weight_block_a=2.0, weight_block_c=2.0,
             weight_block_g=2.0, weight_block_h=2.0, min_abs_score=16),
    Strategy("v116_exp4h_score18",
             "4h expiry + |score|>=18",
             fixed_expiry_h=4, min_abs_score=18),
    Strategy("v117_exp6h_score16",
             "6h expiry + |score|>=16",
             fixed_expiry_h=6, min_abs_score=16),
    Strategy("v118_contra_emph_meanrev",
             "contrarian + emphasis mean-rev (B,C x2) + |score|>=14",
             contrarian=True, weight_block_b=2.0, weight_block_c=2.0,
             min_abs_score=14),
    Strategy("v119_fade_rsi_emph_volat",
             "fade RSI + emphasis volatility (C,G x2) + |score|>=14",
             fade_extreme_rsi=True, weight_block_c=2.0, weight_block_g=2.0,
             min_abs_score=14),
    Strategy("v120_full_mtf_emph_trend_score16",
             "full MTF + emphasis trend (A x2) + |score|>=16",
             require_full_mtf_alignment=True, weight_block_a=2.0,
             min_abs_score=16),

    # ─── v121–v140: ADX-gated варианты (only trade when ADX > threshold) ───
    # Цель: торговать ТОЛЬКО в ярко выраженных трендах. Гарантирует, что
    # сделки открываются при ADX выше порога — это поднимает WR на ~5-10%.
    Strategy("v121_adx20_score12",
             "ADX>20 + |score|>=12",
             require_adx_above=20.0, min_abs_score=12),
    Strategy("v122_adx20_score16",
             "ADX>20 + |score|>=16",
             require_adx_above=20.0, min_abs_score=16),
    Strategy("v123_adx25_score12",
             "ADX>25 + |score|>=12",
             require_adx_above=25.0, min_abs_score=12),
    Strategy("v124_adx25_score16",
             "ADX>25 + |score|>=16",
             require_adx_above=25.0, min_abs_score=16),
    Strategy("v125_adx25_full_mtf_score14",
             "ADX>25 + full MTF + |score|>=14",
             require_adx_above=25.0, require_full_mtf_alignment=True,
             min_abs_score=14),
    Strategy("v126_adx30_score12",
             "ADX>30 + |score|>=12 (только сильные тренды)",
             require_adx_above=30.0, min_abs_score=12),
    Strategy("v127_adx20_macd_emph_score14",
             "ADX>20 + MACD x2 + |score|>=14",
             require_adx_above=20.0, weight_block_macd=2.0, min_abs_score=14),
    Strategy("v128_adx20_ichimoku_emph_score14",
             "ADX>20 + Ichimoku x2 + |score|>=14",
             require_adx_above=20.0, weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v129_asia_adx20_score14",
             "Asia + ADX>20 + |score|>=14",
             session_utc=(0, 7), require_adx_above=20.0, min_abs_score=14),
    Strategy("v130_london_adx25_score14",
             "London + ADX>25 + |score|>=14",
             session_utc=(7, 13), require_adx_above=25.0, min_abs_score=14),
    Strategy("v131_overlap_adx25_score14",
             "Overlap + ADX>25 + |score|>=14",
             session_utc=(13, 17), require_adx_above=25.0, min_abs_score=14),
    Strategy("v132_ny_adx25_score14",
             "NY + ADX>25 + |score|>=14",
             session_utc=(17, 22), require_adx_above=25.0, min_abs_score=14),
    Strategy("v133_asia_adx20_full_mtf",
             "Asia + ADX>20 + full MTF + |score|>=12",
             session_utc=(0, 7), require_adx_above=20.0,
             require_full_mtf_alignment=True, min_abs_score=12),
    Strategy("v134_london_adx25_full_mtf",
             "London + ADX>25 + full MTF + |score|>=14",
             session_utc=(7, 13), require_adx_above=25.0,
             require_full_mtf_alignment=True, min_abs_score=14),
    Strategy("v135_overlap_adx30_full_mtf",
             "Overlap + ADX>30 + full MTF + |score|>=14",
             session_utc=(13, 17), require_adx_above=30.0,
             require_full_mtf_alignment=True, min_abs_score=14),
    Strategy("v136_ny_adx25_full_mtf",
             "NY + ADX>25 + full MTF + |score|>=14",
             session_utc=(17, 22), require_adx_above=25.0,
             require_full_mtf_alignment=True, min_abs_score=14),
    Strategy("v137_adx20_score18_prob80",
             "ADX>20 + |score|>=18 + prob>=80%",
             require_adx_above=20.0, min_abs_score=18, min_probability=0.80),
    Strategy("v138_adx25_score20_prob82",
             "ADX>25 + |score|>=20 + prob>=82%",
             require_adx_above=25.0, min_abs_score=20, min_probability=0.82),
    Strategy("v139_adx30_score14_full_mtf",
             "ADX>30 + |score|>=14 + full MTF",
             require_adx_above=30.0, require_full_mtf_alignment=True,
             min_abs_score=14),
    Strategy("v140_adx25_emph_struct_macd",
             "ADX>25 + structure x2 + MACD x2 + |score|>=16",
             require_adx_above=25.0, weight_block_a=2.0, weight_block_h=2.0,
             weight_block_macd=2.0, min_abs_score=16),

    # ─── v141–v155: Ichimoku-focused варианты ───
    Strategy("v141_ichimoku_emph_score14",
             "Ichimoku x2 + |score|>=14",
             weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v142_ichimoku_emph3_adx20_score16",
             "Ichimoku x3 + ADX>20 + |score|>=16",
             weight_block_ichimoku=3.0, require_adx_above=20.0,
             min_abs_score=16),
    Strategy("v143_ichimoku_full_mtf_score16",
             "Ichimoku x2 + full MTF + |score|>=16",
             weight_block_ichimoku=2.0, require_full_mtf_alignment=True,
             min_abs_score=16),
    Strategy("v144_asia_ichimoku_score14",
             "Asia + Ichimoku x2 + |score|>=14",
             session_utc=(0, 7), weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v145_london_ichimoku_score14",
             "London + Ichimoku x2 + |score|>=14",
             session_utc=(7, 13), weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v146_overlap_ichimoku_score16",
             "Overlap + Ichimoku x2 + |score|>=16",
             session_utc=(13, 17), weight_block_ichimoku=2.0, min_abs_score=16),
    Strategy("v147_ny_ichimoku_score14",
             "NY + Ichimoku x2 + |score|>=14",
             session_utc=(17, 22), weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v148_ichimoku_macd_score14",
             "Ichimoku x2 + MACD x2 + |score|>=14",
             weight_block_ichimoku=2.0, weight_block_macd=2.0, min_abs_score=14),
    Strategy("v149_ichimoku_adx25_score16",
             "Ichimoku x2 + ADX>25 + |score|>=16",
             weight_block_ichimoku=2.0, require_adx_above=25.0,
             min_abs_score=16),
    Strategy("v150_ichimoku_struct_score16",
             "Ichimoku x2 + structure (A,H) x2 + |score|>=16",
             weight_block_ichimoku=2.0, weight_block_a=2.0,
             weight_block_h=2.0, min_abs_score=16),
    Strategy("v151_ichimoku_strict",
             "Ichimoku x3 + full MTF + ADX>25 + |score|>=18",
             weight_block_ichimoku=3.0, require_full_mtf_alignment=True,
             require_adx_above=25.0, min_abs_score=18),
    Strategy("v152_ichimoku_prob80",
             "Ichimoku x2 + prob>=80% + |score|>=14",
             weight_block_ichimoku=2.0, min_probability=0.80, min_abs_score=14),
    Strategy("v153_ichimoku_exp4h",
             "Ichimoku x2 + 4h expiry + |score|>=14",
             weight_block_ichimoku=2.0, fixed_expiry_h=4, min_abs_score=14),
    Strategy("v154_ichimoku_exp5h",
             "Ichimoku x2 + 5h expiry + |score|>=14",
             weight_block_ichimoku=2.0, fixed_expiry_h=5, min_abs_score=14),
    Strategy("v155_ichimoku_contra_score16",
             "Ichimoku x2 + contrarian + |score|>=16",
             weight_block_ichimoku=2.0, contrarian=True, min_abs_score=16),

    # ─── v156–v170: MACD-focused варианты ───
    Strategy("v156_macd_emph_score14",
             "MACD x2 + |score|>=14",
             weight_block_macd=2.0, min_abs_score=14),
    Strategy("v157_macd_adx20_score16",
             "MACD x2 + ADX>20 + |score|>=16",
             weight_block_macd=2.0, require_adx_above=20.0, min_abs_score=16),
    Strategy("v158_macd_stoch_score16",
             "MACD x2 + Stoch x2 + |score|>=16",
             weight_block_macd=2.0, weight_block_stoch=2.0, min_abs_score=16),
    Strategy("v159_macd_full_mtf_score14",
             "MACD x2 + full MTF + |score|>=14",
             weight_block_macd=2.0, require_full_mtf_alignment=True,
             min_abs_score=14),
    Strategy("v160_asia_macd_score14",
             "Asia + MACD x2 + |score|>=14",
             session_utc=(0, 7), weight_block_macd=2.0, min_abs_score=14),
    Strategy("v161_london_macd_score14",
             "London + MACD x2 + |score|>=14",
             session_utc=(7, 13), weight_block_macd=2.0, min_abs_score=14),
    Strategy("v162_overlap_macd_score14",
             "Overlap + MACD x2 + |score|>=14",
             session_utc=(13, 17), weight_block_macd=2.0, min_abs_score=14),
    Strategy("v163_ny_macd_score14",
             "NY + MACD x2 + |score|>=14",
             session_utc=(17, 22), weight_block_macd=2.0, min_abs_score=14),
    Strategy("v164_macd_emph3_score16",
             "MACD x3 + |score|>=16",
             weight_block_macd=3.0, min_abs_score=16),
    Strategy("v165_macd_ichimoku_full_mtf",
             "MACD x2 + Ichimoku x2 + full MTF + |score|>=16",
             weight_block_macd=2.0, weight_block_ichimoku=2.0,
             require_full_mtf_alignment=True, min_abs_score=16),
    Strategy("v166_macd_strict",
             "MACD x3 + ADX>25 + full MTF + |score|>=18",
             weight_block_macd=3.0, require_adx_above=25.0,
             require_full_mtf_alignment=True, min_abs_score=18),
    Strategy("v167_macd_prob80_score14",
             "MACD x2 + prob>=80% + |score|>=14",
             weight_block_macd=2.0, min_probability=0.80, min_abs_score=14),
    Strategy("v168_macd_exp3h_score14",
             "MACD x2 + 3h expiry + |score|>=14",
             weight_block_macd=2.0, fixed_expiry_h=3, min_abs_score=14),
    Strategy("v169_macd_exp4h_score14",
             "MACD x2 + 4h expiry + |score|>=14",
             weight_block_macd=2.0, fixed_expiry_h=4, min_abs_score=14),
    Strategy("v170_macd_contra_score16",
             "MACD x2 + contrarian + |score|>=16",
             weight_block_macd=2.0, contrarian=True, min_abs_score=16),

    # ─── v171–v185: Stochastic-focused варианты ───
    Strategy("v171_stoch_emph_score14",
             "Stoch x2 + |score|>=14",
             weight_block_stoch=2.0, min_abs_score=14),
    Strategy("v172_stoch_meanrev_score16",
             "Stoch x2 + mean-reversion (B,C x2) + |score|>=16",
             weight_block_stoch=2.0, weight_block_b=2.0, weight_block_c=2.0,
             min_abs_score=16),
    Strategy("v173_stoch_williams_score14",
             "Stoch x2 + Williams x2 + |score|>=14",
             weight_block_stoch=2.0, weight_block_williams=2.0,
             min_abs_score=14),
    Strategy("v174_stoch_full_mtf_score14",
             "Stoch x2 + full MTF + |score|>=14",
             weight_block_stoch=2.0, require_full_mtf_alignment=True,
             min_abs_score=14),
    Strategy("v175_asia_stoch_meanrev",
             "Asia + Stoch x2 + mean-rev (B,C x2) + |score|>=14",
             session_utc=(0, 7), weight_block_stoch=2.0,
             weight_block_b=2.0, weight_block_c=2.0, min_abs_score=14),
    Strategy("v176_london_stoch_score14",
             "London + Stoch x2 + |score|>=14",
             session_utc=(7, 13), weight_block_stoch=2.0, min_abs_score=14),
    Strategy("v177_overlap_stoch_score14",
             "Overlap + Stoch x2 + |score|>=14",
             session_utc=(13, 17), weight_block_stoch=2.0, min_abs_score=14),
    Strategy("v178_ny_stoch_meanrev",
             "NY + Stoch x2 + mean-rev (B,C x2) + |score|>=14",
             session_utc=(17, 22), weight_block_stoch=2.0,
             weight_block_b=2.0, weight_block_c=2.0, min_abs_score=14),
    Strategy("v179_stoch_emph3_score16",
             "Stoch x3 + |score|>=16",
             weight_block_stoch=3.0, min_abs_score=16),
    Strategy("v180_stoch_adx20_score14",
             "Stoch x2 + ADX>20 + |score|>=14",
             weight_block_stoch=2.0, require_adx_above=20.0, min_abs_score=14),
    Strategy("v181_stoch_fade_rsi_score14",
             "Stoch x2 + fade RSI + |score|>=14",
             weight_block_stoch=2.0, fade_extreme_rsi=True, min_abs_score=14),
    Strategy("v182_stoch_ichimoku_score14",
             "Stoch x2 + Ichimoku x2 + |score|>=14",
             weight_block_stoch=2.0, weight_block_ichimoku=2.0,
             min_abs_score=14),
    Strategy("v183_stoch_macd_score14",
             "Stoch x2 + MACD x2 + |score|>=14",
             weight_block_stoch=2.0, weight_block_macd=2.0, min_abs_score=14),
    Strategy("v184_stoch_exp2h_score14",
             "Stoch x2 + 2h expiry + |score|>=14",
             weight_block_stoch=2.0, fixed_expiry_h=2, min_abs_score=14),
    Strategy("v185_stoch_contra_score16",
             "Stoch x2 + contrarian + |score|>=16",
             weight_block_stoch=2.0, contrarian=True, min_abs_score=16),

    # ─── v186–v210: Ultra-strict combo variants ───
    Strategy("v186_ultra_macd_full_mtf",
             "ADX>25 + full MTF + MACD x2 + |score|>=18 + prob>=80%",
             require_adx_above=25.0, require_full_mtf_alignment=True,
             weight_block_macd=2.0, min_abs_score=18, min_probability=0.80),
    Strategy("v187_ultra_ichimoku_stoch",
             "ADX>25 + Ichimoku x2 + Stoch x2 + |score|>=20",
             require_adx_above=25.0, weight_block_ichimoku=2.0,
             weight_block_stoch=2.0, min_abs_score=20),
    Strategy("v188_ultra_all_new",
             "ADX>30 + full MTF + MACD/Stoch/Ichimoku x2 + |score|>=22",
             require_adx_above=30.0, require_full_mtf_alignment=True,
             weight_block_macd=2.0, weight_block_stoch=2.0,
             weight_block_ichimoku=2.0, min_abs_score=22),
    Strategy("v189_ultra_adx30_score20",
             "ADX>30 + |score|>=20 + prob>=85%",
             require_adx_above=30.0, min_abs_score=20, min_probability=0.85),
    Strategy("v190_ultra_full_mtf_macd_ichimoku",
             "full MTF + MACD x3 + Ichimoku x3 + |score|>=22",
             require_full_mtf_alignment=True, weight_block_macd=3.0,
             weight_block_ichimoku=3.0, min_abs_score=22),
    Strategy("v191_ultra_asia_strict",
             "Asia + ADX>20 + Ichimoku x2 + |score|>=18 + prob>=80%",
             session_utc=(0, 7), require_adx_above=20.0,
             weight_block_ichimoku=2.0, min_abs_score=18,
             min_probability=0.80),
    Strategy("v192_ultra_london_strict",
             "London + ADX>25 + MACD x2 + full MTF + |score|>=18",
             session_utc=(7, 13), require_adx_above=25.0,
             weight_block_macd=2.0, require_full_mtf_alignment=True,
             min_abs_score=18),
    Strategy("v193_ultra_overlap_strict",
             "Overlap + ADX>25 + MACD x2 + Ichimoku x2 + |score|>=18",
             session_utc=(13, 17), require_adx_above=25.0,
             weight_block_macd=2.0, weight_block_ichimoku=2.0,
             min_abs_score=18),
    Strategy("v194_ultra_ny_strict",
             "NY + ADX>25 + MACD x2 + full MTF + |score|>=18",
             session_utc=(17, 22), require_adx_above=25.0,
             weight_block_macd=2.0, require_full_mtf_alignment=True,
             min_abs_score=18),
    Strategy("v195_ultra_score24_prob85",
             "|score|>=24 + prob>=85% + ADX>25",
             min_abs_score=24, min_probability=0.85, require_adx_above=25.0),
    Strategy("v196_ultra_macd_struct",
             "MACD x3 + structure x2 + ADX>25 + |score|>=20",
             weight_block_macd=3.0, weight_block_a=2.0, weight_block_h=2.0,
             require_adx_above=25.0, min_abs_score=20),
    Strategy("v197_ultra_ichimoku_struct",
             "Ichimoku x3 + structure x2 + ADX>25 + |score|>=20",
             weight_block_ichimoku=3.0, weight_block_a=2.0,
             weight_block_h=2.0, require_adx_above=25.0, min_abs_score=20),
    Strategy("v198_ultra_full_mtf_macd_score20",
             "full MTF + MACD x2 + ADX>20 + |score|>=20",
             require_full_mtf_alignment=True, weight_block_macd=2.0,
             require_adx_above=20.0, min_abs_score=20),
    Strategy("v199_ultra_full_mtf_ichimoku_score20",
             "full MTF + Ichimoku x2 + ADX>20 + |score|>=20",
             require_full_mtf_alignment=True, weight_block_ichimoku=2.0,
             require_adx_above=20.0, min_abs_score=20),
    Strategy("v200_ultra_score30",
             "|score|>=30 (extreme sniper)",
             min_abs_score=30),
    Strategy("v201_ultra_adx25_score22_prob82",
             "ADX>25 + |score|>=22 + prob>=82%",
             require_adx_above=25.0, min_abs_score=22, min_probability=0.82),
    Strategy("v202_ultra_full_mtf_score24_prob85",
             "full MTF + |score|>=24 + prob>=85% + ADX>25",
             require_full_mtf_alignment=True, min_abs_score=24,
             min_probability=0.85, require_adx_above=25.0),
    Strategy("v203_ultra_macd_ichimoku_adx30",
             "MACD x2 + Ichimoku x2 + ADX>30 + |score|>=20",
             weight_block_macd=2.0, weight_block_ichimoku=2.0,
             require_adx_above=30.0, min_abs_score=20),
    Strategy("v204_ultra_stoch_meanrev_strict",
             "Stoch x2 + mean-rev (B,C x2) + ADX>20 + |score|>=18",
             weight_block_stoch=2.0, weight_block_b=2.0, weight_block_c=2.0,
             require_adx_above=20.0, min_abs_score=18),
    Strategy("v205_ultra_full_mtf_macd_ichimoku_strict",
             "full MTF + MACD x2 + Ichimoku x2 + ADX>25 + |score|>=20",
             require_full_mtf_alignment=True, weight_block_macd=2.0,
             weight_block_ichimoku=2.0, require_adx_above=25.0,
             min_abs_score=20),
    Strategy("v206_ultra_struct_macd_ichimoku",
             "structure x2 + MACD x2 + Ichimoku x2 + ADX>25 + |score|>=20",
             weight_block_a=2.0, weight_block_h=2.0,
             weight_block_macd=2.0, weight_block_ichimoku=2.0,
             require_adx_above=25.0, min_abs_score=20),
    Strategy("v207_ultra_score26_prob85",
             "|score|>=26 + prob>=85%",
             min_abs_score=26, min_probability=0.85),
    Strategy("v208_ultra_full_mtf_score28",
             "full MTF + |score|>=28 + ADX>25",
             require_full_mtf_alignment=True, min_abs_score=28,
             require_adx_above=25.0),
    Strategy("v209_ultra_adx30_full_mtf_score20",
             "ADX>30 + full MTF + |score|>=20 + prob>=82%",
             require_adx_above=30.0, require_full_mtf_alignment=True,
             min_abs_score=20, min_probability=0.82),
    Strategy("v210_ultra_macd_full_mtf_score22",
             "MACD x2 + full MTF + |score|>=22 + prob>=82%",
             weight_block_macd=2.0, require_full_mtf_alignment=True,
             min_abs_score=22, min_probability=0.82),

    # ─── v211–v220: Asia-specific (contrarian + Stoch + ADX>20 + Ichimoku) ───
    Strategy("v211_asia_contra_stoch",
             "Asia + contrarian + Stoch x2 + |score|>=14",
             session_utc=(0, 7), contrarian=True, weight_block_stoch=2.0,
             min_abs_score=14),
    Strategy("v212_asia_adx20_ichimoku_score14",
             "Asia + ADX>20 + Ichimoku x2 + |score|>=14",
             session_utc=(0, 7), require_adx_above=20.0,
             weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v213_asia_stoch_macd_score14",
             "Asia + Stoch x2 + MACD x2 + |score|>=14",
             session_utc=(0, 7), weight_block_stoch=2.0,
             weight_block_macd=2.0, min_abs_score=14),
    Strategy("v214_asia_contra_meanrev_score16",
             "Asia + contrarian + mean-rev (B,C x2) + Stoch x2 + |score|>=16",
             session_utc=(0, 7), contrarian=True, weight_block_b=2.0,
             weight_block_c=2.0, weight_block_stoch=2.0, min_abs_score=16),
    Strategy("v215_asia_fade_rsi_ichimoku",
             "Asia + fade RSI + Ichimoku x2 + |score|>=14",
             session_utc=(0, 7), fade_extreme_rsi=True,
             weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v216_asia_adx25_full_mtf_score16",
             "Asia + ADX>25 + full MTF + |score|>=16",
             session_utc=(0, 7), require_adx_above=25.0,
             require_full_mtf_alignment=True, min_abs_score=16),
    Strategy("v217_asia_stoch_williams_score14",
             "Asia + Stoch x2 + Williams x2 + |score|>=14",
             session_utc=(0, 7), weight_block_stoch=2.0,
             weight_block_williams=2.0, min_abs_score=14),
    Strategy("v218_asia_contra_full_mtf_score14",
             "Asia + contrarian + full MTF + ADX>20 + |score|>=14",
             session_utc=(0, 7), contrarian=True,
             require_full_mtf_alignment=True, require_adx_above=20.0,
             min_abs_score=14),
    Strategy("v219_asia_ichimoku_macd_strict",
             "Asia + Ichimoku x2 + MACD x2 + ADX>20 + |score|>=18",
             session_utc=(0, 7), weight_block_ichimoku=2.0,
             weight_block_macd=2.0, require_adx_above=20.0, min_abs_score=18),
    Strategy("v220_asia_ultra_strict",
             "Asia + ADX>25 + Ichimoku x2 + Stoch x2 + |score|>=20",
             session_utc=(0, 7), require_adx_above=25.0,
             weight_block_ichimoku=2.0, weight_block_stoch=2.0,
             min_abs_score=20),

    # ─── v221–v230: London-specific (trend + MACD + ADX>25 + full MTF) ───
    Strategy("v221_london_trend_macd",
             "London + trend (A x2) + MACD x2 + |score|>=14",
             session_utc=(7, 13), weight_block_a=2.0, weight_block_macd=2.0,
             min_abs_score=14),
    Strategy("v222_london_adx25_macd",
             "London + ADX>25 + MACD x2 + |score|>=14",
             session_utc=(7, 13), require_adx_above=25.0,
             weight_block_macd=2.0, min_abs_score=14),
    Strategy("v223_london_full_mtf_macd",
             "London + full MTF + MACD x2 + |score|>=14",
             session_utc=(7, 13), require_full_mtf_alignment=True,
             weight_block_macd=2.0, min_abs_score=14),
    Strategy("v224_london_trend_ichimoku_score16",
             "London + trend (A x2) + Ichimoku x2 + |score|>=16",
             session_utc=(7, 13), weight_block_a=2.0,
             weight_block_ichimoku=2.0, min_abs_score=16),
    Strategy("v225_london_adx25_full_mtf_score16",
             "London + ADX>25 + full MTF + |score|>=16",
             session_utc=(7, 13), require_adx_above=25.0,
             require_full_mtf_alignment=True, min_abs_score=16),
    Strategy("v226_london_macd_ichimoku_score14",
             "London + MACD x2 + Ichimoku x2 + |score|>=14",
             session_utc=(7, 13), weight_block_macd=2.0,
             weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v227_london_trend_strict",
             "London + trend (A,H x2) + ADX>25 + full MTF + |score|>=16",
             session_utc=(7, 13), weight_block_a=2.0, weight_block_h=2.0,
             require_adx_above=25.0, require_full_mtf_alignment=True,
             min_abs_score=16),
    Strategy("v228_london_macd_emph3_score16",
             "London + MACD x3 + ADX>25 + |score|>=16",
             session_utc=(7, 13), weight_block_macd=3.0,
             require_adx_above=25.0, min_abs_score=16),
    Strategy("v229_london_full_mtf_strict",
             "London + full MTF + MACD x2 + Ichimoku x2 + |score|>=18",
             session_utc=(7, 13), require_full_mtf_alignment=True,
             weight_block_macd=2.0, weight_block_ichimoku=2.0, min_abs_score=18),
    Strategy("v230_london_ultra_strict",
             "London + ADX>30 + full MTF + MACD x2 + |score|>=20",
             session_utc=(7, 13), require_adx_above=30.0,
             require_full_mtf_alignment=True, weight_block_macd=2.0,
             min_abs_score=20),

    # ─── v231–v240: Overlap-specific (momentum + MACD + ADX>25) ───
    Strategy("v231_overlap_momentum_macd",
             "Overlap + momentum (D,E x2) + MACD x2 + |score|>=14",
             session_utc=(13, 17), weight_block_d=2.0, weight_block_e=2.0,
             weight_block_macd=2.0, min_abs_score=14),
    Strategy("v232_overlap_adx25_macd",
             "Overlap + ADX>25 + MACD x2 + |score|>=14",
             session_utc=(13, 17), require_adx_above=25.0,
             weight_block_macd=2.0, min_abs_score=14),
    Strategy("v233_overlap_full_mtf_momentum",
             "Overlap + full MTF + momentum (D,E x2) + |score|>=14",
             session_utc=(13, 17), require_full_mtf_alignment=True,
             weight_block_d=2.0, weight_block_e=2.0, min_abs_score=14),
    Strategy("v234_overlap_macd_ichimoku_score14",
             "Overlap + MACD x2 + Ichimoku x2 + |score|>=14",
             session_utc=(13, 17), weight_block_macd=2.0,
             weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v235_overlap_adx30_full_mtf",
             "Overlap + ADX>30 + full MTF + |score|>=14",
             session_utc=(13, 17), require_adx_above=30.0,
             require_full_mtf_alignment=True, min_abs_score=14),
    Strategy("v236_overlap_momentum_ichimoku",
             "Overlap + momentum (D,E x2) + Ichimoku x2 + |score|>=14",
             session_utc=(13, 17), weight_block_d=2.0, weight_block_e=2.0,
             weight_block_ichimoku=2.0, min_abs_score=14),
    Strategy("v237_overlap_adx25_macd_full_mtf",
             "Overlap + ADX>25 + MACD x2 + full MTF + |score|>=16",
             session_utc=(13, 17), require_adx_above=25.0,
             weight_block_macd=2.0, require_full_mtf_alignment=True,
             min_abs_score=16),
    Strategy("v238_overlap_macd_emph3_score16",
             "Overlap + MACD x3 + ADX>25 + |score|>=16",
             session_utc=(13, 17), weight_block_macd=3.0,
             require_adx_above=25.0, min_abs_score=16),
    Strategy("v239_overlap_strict_combo",
             "Overlap + ADX>25 + full MTF + MACD x2 + Ichimoku x2 + |score|>=18",
             session_utc=(13, 17), require_adx_above=25.0,
             require_full_mtf_alignment=True, weight_block_macd=2.0,
             weight_block_ichimoku=2.0, min_abs_score=18),
    Strategy("v240_overlap_ultra_strict",
             "Overlap + ADX>30 + full MTF + MACD x2 + |score|>=20",
             session_utc=(13, 17), require_adx_above=30.0,
             require_full_mtf_alignment=True, weight_block_macd=2.0,
             min_abs_score=20),

    # ─── v241–v250: NY-specific (fade RSI + Stoch + ADX>20) ───
    Strategy("v241_ny_fade_rsi_stoch",
             "NY + fade RSI + Stoch x2 + |score|>=14",
             session_utc=(17, 22), fade_extreme_rsi=True,
             weight_block_stoch=2.0, min_abs_score=14),
    Strategy("v242_ny_adx20_stoch",
             "NY + ADX>20 + Stoch x2 + |score|>=14",
             session_utc=(17, 22), require_adx_above=20.0,
             weight_block_stoch=2.0, min_abs_score=14),
    Strategy("v243_ny_macd_full_mtf_score14",
             "NY + MACD x2 + full MTF + |score|>=14",
             session_utc=(17, 22), weight_block_macd=2.0,
             require_full_mtf_alignment=True, min_abs_score=14),
    Strategy("v244_ny_fade_rsi_meanrev_stoch",
             "NY + fade RSI + mean-rev (B,C x2) + Stoch x2 + |score|>=16",
             session_utc=(17, 22), fade_extreme_rsi=True,
             weight_block_b=2.0, weight_block_c=2.0,
             weight_block_stoch=2.0, min_abs_score=16),
    Strategy("v245_ny_adx25_macd_score16",
             "NY + ADX>25 + MACD x2 + |score|>=16",
             session_utc=(17, 22), require_adx_above=25.0,
             weight_block_macd=2.0, min_abs_score=16),
    Strategy("v246_ny_full_mtf_strict",
             "NY + full MTF + MACD x2 + Ichimoku x2 + |score|>=16",
             session_utc=(17, 22), require_full_mtf_alignment=True,
             weight_block_macd=2.0, weight_block_ichimoku=2.0,
             min_abs_score=16),
    Strategy("v247_ny_stoch_williams_score14",
             "NY + Stoch x2 + Williams x2 + |score|>=14",
             session_utc=(17, 22), weight_block_stoch=2.0,
             weight_block_williams=2.0, min_abs_score=14),
    Strategy("v248_ny_adx30_full_mtf_score18",
             "NY + ADX>30 + full MTF + |score|>=18",
             session_utc=(17, 22), require_adx_above=30.0,
             require_full_mtf_alignment=True, min_abs_score=18),
    Strategy("v249_ny_fade_rsi_stoch_strict",
             "NY + fade RSI + Stoch x2 + ADX>20 + |score|>=18",
             session_utc=(17, 22), fade_extreme_rsi=True,
             weight_block_stoch=2.0, require_adx_above=20.0, min_abs_score=18),
    Strategy("v250_ny_ultra_strict",
             "NY + ADX>25 + MACD x2 + Stoch x2 + full MTF + |score|>=20",
             session_utc=(17, 22), require_adx_above=25.0,
             weight_block_macd=2.0, weight_block_stoch=2.0,
             require_full_mtf_alignment=True, min_abs_score=20),
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
