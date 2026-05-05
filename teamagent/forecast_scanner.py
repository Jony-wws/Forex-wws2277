"""Forecast scanner — ЕДИНЫЙ источник правды для 28 пар × 5-мин цикл.

Никакого «отдельно мета-голосование, отдельно ПРОГНОЗЫ» — теперь это ОДНА таблица.
Каждый прогноз содержит:
- pair, side (BUY/SELL), probability (capped 50–92%)
- recommended_hours (1–4)
- score (вклад каждого правила, для прозрачности)
- agents_for / agents_against (мета-голосование интегрировано сюда же)
- volume_profile snapshot
- timestamp
"""
from __future__ import annotations
import json
import logging
import math
import os
import time
import signal
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from . import config, indicators, volume_profile
from .data import yahoo, news

log = logging.getLogger("scanner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "forecast_scanner.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

FORECASTS_FILE = config.STATE_DIR / "forecasts.json"
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_forecast_scanner.json"


def _heartbeat() -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "forecast_scanner",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": __import__("os").getpid(),
    }))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _score_to_probability(score: int, max_score: int | None = None) -> float:
    """Score → probability 0..1, абсолютная.

    Score>0 → BUY вероятность; <0 → SELL вероятность.
    max_score defaults to config.MAX_SCORE (95) to account for all indicator
    blocks including Phase 15 additions (CCI, ROC, Pivot, PSAR, S/R,
    consecutive candles, session time).
    """
    if max_score is None:
        max_score = getattr(config, "MAX_SCORE", 95)
    norm = score / max_score              # -1..+1
    p = _sigmoid(norm * 4.0)              # softer sigmoid
    return p


def _time_until_midnight_utc() -> float:
    """Hours remaining until 0:00 UTC (next midnight)."""
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds() / 3600.0


def _session_remaining_hours(now: datetime) -> float:
    """Hours remaining in current trading session. Returns 0 if off-hours."""
    hour = now.hour
    for name, (lo, hi) in config.SESSIONS.items():
        if lo <= hour <= hi:
            remaining = hi - hour + (60 - now.minute) / 60.0
            return max(0, remaining)
    return 0.0


_strategy_cache: dict | None = None
_strategy_cache_ts: float = 0.0


def _load_strategy_config() -> dict:
    """Load strategy_config.json with 60-second cache."""
    global _strategy_cache, _strategy_cache_ts
    now = time.time()
    if _strategy_cache is not None and now - _strategy_cache_ts < 60:
        return _strategy_cache
    sc_file = config.STATE_DIR / "strategy_config.json"
    locked_file = config.STATE_DIR / "strategy_config_locked.json"
    for f in (sc_file, locked_file):
        if f.exists():
            try:
                data = json.loads(f.read_text())
                _strategy_cache = data
                _strategy_cache_ts = now
                return data
            except Exception:
                continue
    return {}


def _get_strategy_wr(pair: str, session: str) -> float | None:
    """Get the best strategy WR for a pair in the current session.

    Returns the best WR as a fraction (0.0-1.0), checking:
    1. By-session WR for the current session
    2. Global best WR for the pair
    Picks the highest available."""
    sc = _load_strategy_config()
    pairs = sc.get("pairs", {})
    pair_data = pairs.get(pair, {})
    if not pair_data:
        return None

    candidates: list[float] = []

    # Session-specific WR
    by_session = pair_data.get("by_session", {})
    sess_data = by_session.get(session, {})
    if sess_data.get("win_rate_pct"):
        candidates.append(float(sess_data["win_rate_pct"]) / 100.0)

    # Global best WR
    if pair_data.get("win_rate_pct"):
        candidates.append(float(pair_data["win_rate_pct"]) / 100.0)

    # Also check all sessions for the best one
    for s_name, s_data in by_session.items():
        if s_data.get("win_rate_pct"):
            candidates.append(float(s_data["win_rate_pct"]) / 100.0)

    if not candidates:
        return None
    return max(candidates)


def _confluence_ratio(score_breakdown: list[dict]) -> float:
    """Calculate what fraction of voting indicators agree with the majority direction.
    Returns 0.0-1.0. Higher = stronger confluence."""
    if not score_breakdown:
        return 0.0
    bullish = sum(1 for s in score_breakdown if s["contrib"] > 0)
    bearish = sum(1 for s in score_breakdown if s["contrib"] < 0)
    total = bullish + bearish
    if total == 0:
        return 0.0
    return max(bullish, bearish) / total


def evaluate_pair(pair: str) -> dict | None:
    """Полная оценка одной пары: TF 4H + 1H + 15m + Volume Profile + новости."""
    # Single timestamp reused for session detection, news blackout, scanned_at.
    now = datetime.now(timezone.utc)
    # данные
    bars_4h = yahoo.latest_bars(pair, "1h", 240)        # 240×1h ≈ 10 дней
    bars_1h = yahoo.latest_bars(pair, "1h", 100)
    bars_15m = yahoo.latest_bars(pair, "15m", 100)
    if any(df.empty or len(df) < 30 for df in (bars_4h, bars_1h, bars_15m)):
        log.warning(f"{pair}: not enough bars")
        return None

    ind_4h = indicators.all_indicators(bars_4h)
    ind_1h = indicators.all_indicators(bars_1h)
    ind_15m = indicators.all_indicators(bars_15m)

    if not ind_4h or not ind_1h or not ind_15m:
        return None

    score = 0
    score_breakdown: list[dict] = []
    agents_for: list[str] = []
    agents_against: list[str] = []

    def vote(name: str, contrib: int, reason: str) -> None:
        nonlocal score
        score += contrib
        score_breakdown.append({"name": name, "contrib": contrib, "reason": reason})
        if contrib > 0:
            agents_for.append(name)
        elif contrib < 0:
            agents_against.append(name)

    # ───── BLOCK A — VWAP / EMA / структура старшего TF ─────
    if ind_4h["close"] > ind_4h["ema50"] > ind_4h["ema200"]:
        vote("4H_strong_uptrend", +3, "close > ema50 > ema200 (4H)")
    elif ind_4h["close"] < ind_4h["ema50"] < ind_4h["ema200"]:
        vote("4H_strong_downtrend", -3, "close < ema50 < ema200 (4H)")
    elif ind_4h["close"] > ind_4h["ema50"]:
        vote("4H_uptrend", +1, "close > ema50 (4H)")
    elif ind_4h["close"] < ind_4h["ema50"]:
        vote("4H_downtrend", -1, "close < ema50 (4H)")

    # 1H confirmation
    if ind_1h["close"] > ind_1h["ema20"] > ind_1h["ema50"]:
        vote("1H_uptrend", +2, "close > ema20 > ema50 (1H)")
    elif ind_1h["close"] < ind_1h["ema20"] < ind_1h["ema50"]:
        vote("1H_downtrend", -2, "close < ema20 < ema50 (1H)")

    # 15m alignment / entry
    if ind_15m["close"] > ind_15m["ema20"]:
        vote("15m_above_ema20", +1, "close > ema20 (15m)")
    else:
        vote("15m_below_ema20", -1, "close < ema20 (15m)")

    # ───── BLOCK B — RSI ─────
    if 50 < ind_1h["rsi14"] < 70:
        vote("1H_rsi_bullish", +2, f"RSI={ind_1h['rsi14']:.1f}")
    elif 30 < ind_1h["rsi14"] < 50:
        vote("1H_rsi_bearish", -2, f"RSI={ind_1h['rsi14']:.1f}")
    elif ind_1h["rsi14"] >= 70:
        vote("1H_rsi_overbought", -2, f"RSI={ind_1h['rsi14']:.1f} (откат вниз)")
    elif ind_1h["rsi14"] <= 30:
        vote("1H_rsi_oversold", +2, f"RSI={ind_1h['rsi14']:.1f} (отскок вверх)")

    # ───── BLOCK C — Bollinger %B ─────
    if ind_1h["bb_pct"] > 0.95:
        vote("1H_bb_overbought", -1, f"%B={ind_1h['bb_pct']:.2f}")
    elif ind_1h["bb_pct"] < 0.05:
        vote("1H_bb_oversold", +1, f"%B={ind_1h['bb_pct']:.2f}")
    elif 0.5 < ind_1h["bb_pct"] < 0.85:
        vote("1H_bb_above_mid", +1, f"%B={ind_1h['bb_pct']:.2f}")
    elif 0.15 < ind_1h["bb_pct"] < 0.5:
        vote("1H_bb_below_mid", -1, f"%B={ind_1h['bb_pct']:.2f}")

    # ───── BLOCK D — Momentum ─────
    if ind_1h["mom5"] > 0.1:
        vote("1H_momentum_up", +2, f"mom5={ind_1h['mom5']:.2f}%")
    elif ind_1h["mom5"] < -0.1:
        vote("1H_momentum_down", -2, f"mom5={ind_1h['mom5']:.2f}%")

    # ───── BLOCK E — CEI / OFI ─────
    if ind_1h["cei10"] > 60 and ind_1h["ofi10"] > 0.3:
        vote("1H_strong_bull_candles", +2, f"CEI={ind_1h['cei10']:.0f}% OFI={ind_1h['ofi10']:+.2f}")
    elif ind_1h["cei10"] > 60 and ind_1h["ofi10"] < -0.3:
        vote("1H_strong_bear_candles", -2, f"CEI={ind_1h['cei10']:.0f}% OFI={ind_1h['ofi10']:+.2f}")

    # ───── BLOCK F — VWAP relation ─────
    if ind_1h["close"] > ind_1h["vwap"] * 1.001:
        vote("1H_above_vwap", +1, "close выше VWAP")
    elif ind_1h["close"] < ind_1h["vwap"] * 0.999:
        vote("1H_below_vwap", -1, "close ниже VWAP")

    # ───── BLOCK G — BBP regime ─────
    if ind_1h["bbp"] > 0:
        vote("1H_bbp_bull", +1, f"BBP={ind_1h['bbp']:.5f}")
    elif ind_1h["bbp"] < 0:
        vote("1H_bbp_bear", -1, f"BBP={ind_1h['bbp']:.5f}")

    # ───── BLOCK H — Multi-TF agreement (бонус) ─────
    bull_count = int(ind_4h["close"] > ind_4h["ema50"]) \
               + int(ind_1h["close"] > ind_1h["ema20"]) \
               + int(ind_15m["close"] > ind_15m["ema20"])
    if bull_count == 3:
        vote("MTF_full_bull", +3, "все 3 TF выше EMA")
    elif bull_count == 0:
        vote("MTF_full_bear", -3, "все 3 TF ниже EMA")

    # ───── BLOCK H2 — ADX Regime Gate (флэт-штраф / тренд-бонус) ─────
    adx_val = ind_1h.get("adx", 0.0)
    if adx_val < 15:
        # Флэт — рынок непредсказуем; штрафуем абсолютную силу сигнала.
        penalty = int(round(abs(score) * 0.6))
        if penalty > 0:
            if score > 0:
                vote("ADX_flat_penalty", -penalty,
                     f"ADX={adx_val:.1f} < 15 — флэт, score снижен на 60%")
            elif score < 0:
                vote("ADX_flat_penalty", +penalty,
                     f"ADX={adx_val:.1f} < 15 — флэт, score снижен на 60%")
    elif adx_val > 30:
        plus_di = ind_1h.get("plus_di", 0.0)
        minus_di = ind_1h.get("minus_di", 0.0)
        if plus_di > minus_di:
            vote("ADX_strong_uptrend", +3,
                 f"ADX={adx_val:.1f}>30, +DI>-DI — сильный восходящий тренд")
        else:
            vote("ADX_strong_downtrend", -3,
                 f"ADX={adx_val:.1f}>30, -DI>+DI — сильный нисходящий тренд")

    # ───── BLOCK H3 — MACD Confirmation ─────
    macd_hist = ind_1h.get("macd_hist", 0.0)
    macd_prev = ind_1h.get("macd_prev_hist", 0.0)
    if macd_hist > 0 and macd_prev <= 0:
        vote("MACD_bullish_cross", +2, "MACD гистограмма пересекла 0 вверх")
    elif macd_hist < 0 and macd_prev >= 0:
        vote("MACD_bearish_cross", -2, "MACD гистограмма пересекла 0 вниз")
    elif macd_hist > 0 and macd_hist > macd_prev:
        vote("MACD_bullish_accel", +1, "MACD гистограмма растёт")
    elif macd_hist < 0 and macd_hist < macd_prev:
        vote("MACD_bearish_accel", -1, "MACD гистограмма падает")

    # ───── BLOCK H4 — Stochastic Oscillator ─────
    stoch_k = ind_1h.get("stoch_k", 50.0)
    stoch_d = ind_1h.get("stoch_d", 50.0)
    if stoch_k < 20 and stoch_d < 20:
        vote("Stoch_oversold", +2,
             f"Stoch K={stoch_k:.1f} D={stoch_d:.1f} — перепроданность")
    elif stoch_k > 80 and stoch_d > 80:
        vote("Stoch_overbought", -2,
             f"Stoch K={stoch_k:.1f} D={stoch_d:.1f} — перекупленность")
    elif stoch_k > stoch_d and stoch_k < 80:
        vote("Stoch_bullish", +1, "Stoch K > D (бычий)")
    elif stoch_k < stoch_d and stoch_k > 20:
        vote("Stoch_bearish", -1, "Stoch K < D (медвежий)")

    # ───── BLOCK H5 — Williams %R ─────
    wr_val = ind_1h.get("williams_r", -50.0)
    if wr_val > -20:
        vote("WilliamsR_overbought", -1,
             f"Williams %R={wr_val:.1f} > -20 — перекупленность")
    elif wr_val < -80:
        vote("WilliamsR_oversold", +1,
             f"Williams %R={wr_val:.1f} < -80 — перепроданность")

    # ───── BLOCK H6 — Ichimoku Cloud ─────
    above_cloud = bool(ind_1h.get("ichimoku_above_cloud", 0.0))
    below_cloud = bool(ind_1h.get("ichimoku_below_cloud", 0.0))
    tenkan = ind_1h.get("ichimoku_tenkan", 0.0)
    kijun = ind_1h.get("ichimoku_kijun", 0.0)
    if above_cloud and tenkan > kijun:
        vote("Ichimoku_strong_bull", +3, "Цена выше облака + Tenkan > Kijun")
    elif below_cloud and tenkan < kijun:
        vote("Ichimoku_strong_bear", -3, "Цена ниже облака + Tenkan < Kijun")
    elif above_cloud:
        vote("Ichimoku_bull", +1, "Цена выше облака Ишимоку")
    elif below_cloud:
        vote("Ichimoku_bear", -1, "Цена ниже облака Ишимоку")

    # ───── BLOCK H7 — CCI (Commodity Channel Index) ─────
    cci_val = ind_1h.get("cci20", 0.0)
    if cci_val > 200:
        vote("CCI_extreme_overbought", -3, f"CCI={cci_val:.0f} >200 — сильная перекупленность")
    elif cci_val > 100:
        vote("CCI_overbought", -1, f"CCI={cci_val:.0f} >100 — перекупленность")
    elif cci_val < -200:
        vote("CCI_extreme_oversold", +3, f"CCI={cci_val:.0f} <-200 — сильная перепроданность")
    elif cci_val < -100:
        vote("CCI_oversold", +1, f"CCI={cci_val:.0f} <-100 — перепроданность")
    elif 0 < cci_val < 100:
        vote("CCI_bullish_zone", +1, f"CCI={cci_val:.0f} — бычья зона")
    elif -100 < cci_val < 0:
        vote("CCI_bearish_zone", -1, f"CCI={cci_val:.0f} — медвежья зона")

    # ───── BLOCK H8 — Rate of Change (ROC) ─────
    roc_val = ind_1h.get("roc10", 0.0)
    if roc_val > 0.3:
        vote("ROC_strong_up", +2, f"ROC={roc_val:.3f}% — сильный бычий момент")
    elif roc_val > 0.1:
        vote("ROC_mild_up", +1, f"ROC={roc_val:.3f}% — умеренный бычий момент")
    elif roc_val < -0.3:
        vote("ROC_strong_down", -2, f"ROC={roc_val:.3f}% — сильный медвежий момент")
    elif roc_val < -0.1:
        vote("ROC_mild_down", -1, f"ROC={roc_val:.3f}% — умеренный медвежий момент")

    # ───── BLOCK H9 — Pivot Points (Support/Resistance levels) ─────
    close_now = ind_1h["close"]
    pivot_pp = ind_1h.get("pivot_pp", close_now)
    pivot_r1 = ind_1h.get("pivot_r1", close_now)
    pivot_s1 = ind_1h.get("pivot_s1", close_now)
    pivot_r2 = ind_1h.get("pivot_r2", close_now)
    pivot_s2 = ind_1h.get("pivot_s2", close_now)
    if pivot_pp > 0:
        if close_now > pivot_r1:
            vote("Pivot_above_R1", +2, f"Цена выше R1 ({pivot_r1:.5f}) — бычий пробой")
        elif close_now > pivot_pp:
            vote("Pivot_above_PP", +1, f"Цена выше PP ({pivot_pp:.5f})")
        elif close_now < pivot_s1:
            vote("Pivot_below_S1", -2, f"Цена ниже S1 ({pivot_s1:.5f}) — медвежий пробой")
        elif close_now < pivot_pp:
            vote("Pivot_below_PP", -1, f"Цена ниже PP ({pivot_pp:.5f})")

    # ───── BLOCK H10 — Parabolic SAR ─────
    psar_bull = bool(ind_1h.get("psar_bullish", 0.0))
    psar_bull_4h = bool(ind_4h.get("psar_bullish", 0.0))
    if psar_bull and psar_bull_4h:
        vote("PSAR_full_bull", +2, "PSAR бычий на 1H и 4H")
    elif not psar_bull and not psar_bull_4h:
        vote("PSAR_full_bear", -2, "PSAR медвежий на 1H и 4H")
    elif psar_bull:
        vote("PSAR_1h_bull", +1, "PSAR бычий на 1H")
    else:
        vote("PSAR_1h_bear", -1, "PSAR медвежий на 1H")

    # ───── BLOCK H11 — Support/Resistance proximity ─────
    res_high = ind_1h.get("resistance_high", 0)
    sup_low = ind_1h.get("support_low", 0)
    if res_high > 0 and sup_low > 0:
        range_sr = res_high - sup_low
        if range_sr > 0:
            pos_in_range = (close_now - sup_low) / range_sr
            if pos_in_range > 0.95:
                vote("SR_at_resistance", -2, f"Цена у сопротивления ({pos_in_range:.0%})")
            elif pos_in_range < 0.05:
                vote("SR_at_support", +2, f"Цена у поддержки ({pos_in_range:.0%})")
            elif pos_in_range > 0.80:
                vote("SR_near_resistance", -1, f"Цена близко к сопротивлению ({pos_in_range:.0%})")
            elif pos_in_range < 0.20:
                vote("SR_near_support", +1, f"Цена близко к поддержке ({pos_in_range:.0%})")

    # ───── BLOCK H12 — Consecutive candles (trend quality) ─────
    consec = int(ind_1h.get("consec_candles", 0))
    if consec >= 4:
        vote("Consec_strong_bull_run", +3, f"{consec} бычьих свечей подряд")
    elif consec >= 3:
        vote("Consec_bull_run", +2, f"{consec} бычьих свечей подряд")
    elif consec <= -4:
        vote("Consec_strong_bear_run", -3, f"{abs(consec)} медвежьих свечей подряд")
    elif consec <= -3:
        vote("Consec_bear_run", -2, f"{abs(consec)} медвежьих свечей подряд")

    # ───── BLOCK N — Session Time Awareness ─────
    session_remaining_h = _session_remaining_hours(now)
    hours_to_midnight = _time_until_midnight_utc()
    if session_remaining_h < 1.0 and session_remaining_h > 0:
        penalty = min(3, int(round(abs(score) * 0.3)))
        if penalty > 0:
            if score > 0:
                vote("session_ending_penalty", -penalty,
                     f"До конца сессии {session_remaining_h:.1f}ч — снижаем уверенность")
            elif score < 0:
                vote("session_ending_penalty", +penalty,
                     f"До конца сессии {session_remaining_h:.1f}ч — снижаем уверенность")

    # ───── BLOCK I — Fundamental macro tilt (FRED rates / yields / CPI) ─────
    # Source: teamagent.fundamentals (FRED 24h cache, no API key).
    # Cap ±5 contribution so tech signals dominate; fundamentals are a slow-
    # moving bias on top of the technical engine.
    try:
        from . import fundamentals as fund
        tilt = fund.pair_macro_tilt(pair)
        score_pts = round(tilt.get("tilt_score", 0) / 16.0, 1)  # ±5 cap below
        score_pts = max(-5, min(5, score_pts))
        if abs(score_pts) >= 1:
            vote(
                "fundamental_macro_tilt",
                int(round(score_pts)),
                f"{tilt['side']} (rate_diff={tilt.get('rate_diff_pct')} "
                f"yield_diff={tilt.get('yield_diff_pct')} "
                f"cpi_diff={tilt.get('cpi_diff_pct')}, conf={tilt.get('confidence_pct')}%)",
            )
    except Exception as e:
        log.warning(f"forecast_scanner: fundamental tilt failed for {pair}: {e}")

    # ───── BLOCK J0 — meta_strategy_agent tactical bias (5h cycle) ─────
    # Источник: teamagent.strategy_meta_agent — каждые 5 часов делает sweep
    # 28 пар × 4 сессии × 120 вариантов на 5d окне + ансамбль COT/Fund/Regime/Radar.
    # Если ячейка (pair, current_session) у мета-агента имеет статус
    # QUALIFIED — добавляем +/-3 голос; PROBABLE — +/-2; FROZEN — 0.
    # Знак определяет side_bias из ансамбля (если он 0, голос не идёт).
    try:
        from . import strategy_meta_agent as meta
        from . import strategies as _strats
        # Используем именно strategies.SESSION_WINDOWS (Asia/London/Overlap/NY),
        # так как meta_agent пишет ключи именно в этой нотации. config.SESSIONS
        # использует чуть другое разбиение для UI, и оно НЕ совпадает.
        meta_session = _strats.detect_session(now.hour)
        if meta_session is not None:
            cell = meta.get_cell_for(pair, meta_session)
            if cell:
                status = cell.get("status")
                bias = int(cell.get("side_bias") or 0)
                if status in ("QUALIFIED", "PROBABLE") and bias != 0:
                    pts = 3 if status == "QUALIFIED" else 2
                    pts = pts if bias > 0 else -pts
                    vote(
                        "meta_strategy_agent",
                        pts,
                        f"{status} cell, expected_wr={cell.get('win_rate_pct')}% "
                        f"(wilson_lower={cell.get('wilson_lower_pct')}%) "
                        f"variant={cell.get('variant')} bias={bias}",
                    )
    except Exception as e:
        log.warning(f"forecast_scanner: meta_strategy_agent integration failed for {pair}: {e}")

    # ───── BLOCK J — COT speculator positioning (CFTC weekly) ─────
    # Source: teamagent.cot (CFTC public Socrata API, 24h cache, no key).
    # Contrarian: when leveraged-money funds are stretched long/short
    # (|z|>1.5 over 52w), expect mean-reversion. Cap ±4 score contribution.
    # Read once per scan pass via module-level cache (cheap after first call).
    try:
        from . import cot as cot_mod
        sig = cot_mod.pair_cot_signal(pair)
        if sig.get("side") in ("BUY", "SELL"):
            strength = sig.get("strength_pct", 0)
            pts = max(1, min(4, int(round(strength / 25))))
            if sig["side"] == "SELL":
                pts = -pts
            vote(
                "cot_speculator_contrarian",
                pts,
                f"{sig['side']} (combined_z={sig.get('combined_z')}, "
                f"strength={strength}%) — {sig.get('note')}",
            )
    except Exception as e:
        log.warning(f"forecast_scanner: cot signal failed for {pair}: {e}")

    # ───── PENALTY: news blackout ─────
    # high-impact новость ±30 мин: снижаем confidence обеих сторон,
    # уменьшая abs(score) на величину penalty (но не ниже нуля).
    if news.is_blackout(pair, now):
        penalty = min(config.NEWS_BLACKOUT_PENALTY, abs(score))
        delta = -penalty if score > 0 else (penalty if score < 0 else 0)
        vote("news_blackout", delta, f"high-impact новость ±30 мин — снижаем abs(score) на {penalty}")

    # ───── CONFLUENCE + STRATEGY CALIBRATION (Phase 15) ─────
    confluence = _confluence_ratio(score_breakdown)

    # ───── итог ─────
    if score == 0:
        return None  # нейтрально, не показываем

    side = "BUY" if score > 0 else "SELL"
    max_sc = getattr(config, "MAX_SCORE", 95)
    p_raw = _score_to_probability(abs(score), max_sc)
    # cap 50–92
    p = max(0.50, min(config.MAX_PROBABILITY, p_raw))

    # Confluence bonus: high confluence raises probability
    if confluence >= 0.85:
        p = min(config.MAX_PROBABILITY, p + 0.04)
    elif confluence >= 0.75:
        p = min(config.MAX_PROBABILITY, p + 0.03)
    elif confluence >= 0.65:
        p = min(config.MAX_PROBABILITY, p + 0.02)

    # ───── Strategy backtest WR calibration ─────
    # Blend technical probability with historical strategy WR for accuracy
    strat_wr = _get_strategy_wr(pair, _current_session(now.hour))
    strat_wr_pct = strat_wr * 100 if strat_wr else 0
    if strat_wr is not None and strat_wr > 0:
        # Weighted blend: 40% technical + 60% historical strategy WR
        p_blended = 0.40 * p + 0.60 * strat_wr
        p = max(0.50, min(config.MAX_PROBABILITY, p_blended))

    # Quality tier based on combined signals
    quality_tier = "STRONG"  # >= 75%
    if p < 0.70:
        quality_tier = "WEAK"
    elif p < 0.75:
        quality_tier = "MODERATE"

    # Mathematical EV calculation at user's broker payout
    broker_payout = float(os.environ.get("BROKER_PAYOUT_PCT", "0.85"))
    ev_per_trade = p * broker_payout - (1.0 - p)
    breakeven_wr = 1.0 / (1.0 + broker_payout)

    # рекомендованная экспирация: больше score → дольше держим
    abs_norm = min(1.0, abs(score) / 25.0)
    recommended_hours = int(round(config.MIN_EXPIRY_HOURS + abs_norm * (config.MAX_EXPIRY_HOURS - config.MIN_EXPIRY_HOURS)))
    recommended_hours = max(config.MIN_EXPIRY_HOURS, min(config.MAX_EXPIRY_HOURS, recommended_hours))

    # Time-aware expiry cap: don't set expiry beyond session end
    if session_remaining_h > 0:
        recommended_hours = min(recommended_hours, max(1, int(session_remaining_h)))

    # volume profile snapshot
    try:
        vp = volume_profile.build(pair)
    except Exception as e:
        log.warning(f"VP failed pair={pair}: {e}")
        vp = {"error": str(e)}

    forecast = {
        "pair": pair,
        "side": side,
        "probability": round(p, 4),
        "probability_pct": round(p * 100.0, 1),
        "score": score,
        "max_score": max_sc,
        "recommended_hours": recommended_hours,
        "current_price": ind_15m["close"],
        "confluence_ratio": round(confluence, 3),
        "confluence_pct": round(confluence * 100, 1),
        "session_remaining_hours": round(session_remaining_h, 2),
        "hours_to_midnight_utc": round(hours_to_midnight, 2),
        "quality_tier": quality_tier,
        "strategy_backtest_wr_pct": round(strat_wr_pct, 1),
        "ev_per_trade": round(ev_per_trade, 4),
        "ev_pct": round(ev_per_trade * 100, 1),
        "breakeven_wr_pct": round(breakeven_wr * 100, 1),
        "broker_payout_pct": round(broker_payout * 100, 1),
        "indicators": {
            "4H": ind_4h,
            "1H": ind_1h,
            "15m": ind_15m,
        },
        "score_breakdown": score_breakdown,
        "agents_for": agents_for,
        "agents_against": agents_against,
        "agents_for_count": len(agents_for),
        "agents_against_count": len(agents_against),
        "volume_profile": vp,
        "as_of": now.isoformat(),
        "session": _current_session(now.hour),
    }
    return forecast


def _current_session(hour: int) -> str:
    for name, (lo, hi) in config.SESSIONS.items():
        if lo <= hour <= hi:
            return name
    return "Off"


def scan_all_pairs() -> dict:
    """Полный обход 28 пар. Сохраняет общий snapshot в state/forecasts.json."""
    now = datetime.now(timezone.utc)
    snapshot = {
        "scanned_at": now.isoformat(),
        "total_pairs": len(config.PAIRS),
        "hours_to_midnight_utc": round(_time_until_midnight_utc(), 2),
        "session_remaining_hours": round(_session_remaining_hours(now), 2),
        "current_session": _current_session(now.hour),
        "quality_gate": {
            "min_abs_score": getattr(config, "MIN_ABSOLUTE_SCORE", 8),
            "min_confluence_ratio": getattr(config, "MIN_CONFLUENCE_RATIO", 0.60),
            "min_probability": config.MIN_PROBABILITY,
        },
        "forecasts": {},
        "rankings": [],
    }
    for pair in config.PAIRS:
        try:
            f = evaluate_pair(pair)
        except Exception as e:
            log.exception(f"evaluate_pair failed pair={pair}: {e}")
            f = None
            err_reason = f"exception: {type(e).__name__}"
        else:
            err_reason = "no_data" if f is None else None

        if f is None:
            # Plant a self-describing placeholder so downstream tools know the
            # pair was attempted (system_audit relies on this for invariant
            # `forecasts ↔ config.PAIRS` to stay green even when a single pair
            # is rate-limited).
            snapshot["forecasts"][pair] = {
                "pair": pair,
                "side": "NEUTRAL",
                "probability_pct": 50.0,
                "score": 0.0,
                "skipped": True,
                "skip_reason": err_reason,
                "scanned_at": snapshot["scanned_at"],
            }
            continue
        snapshot["forecasts"][pair] = f
        snapshot["rankings"].append({
            "pair": pair,
            "side": f["side"],
            "probability_pct": f["probability_pct"],
            "score": f["score"],
            "recommended_hours": f["recommended_hours"],
            "confluence_pct": f.get("confluence_pct", 0),
            "quality_tier": f.get("quality_tier", ""),
            "strategy_backtest_wr_pct": f.get("strategy_backtest_wr_pct", 0),
            "ev_pct": f.get("ev_pct", 0),
            "session_remaining_hours": f.get("session_remaining_hours", 0),
            "agents_for_count": f.get("agents_for_count", 0),
            "agents_against_count": f.get("agents_against_count", 0),
        })
    snapshot["rankings"].sort(key=lambda x: x["probability_pct"], reverse=True)
    FORECASTS_FILE.write_text(json.dumps(snapshot, indent=2))
    log.info(
        f"scanned {len(config.PAIRS)} pairs, got {len(snapshot['forecasts'])} forecasts; "
        f"top: {snapshot['rankings'][:3]}"
    )
    return snapshot


def run_loop(interval_sec: int | None = None) -> None:
    interval_sec = interval_sec or config.FORECAST_SCANNER_INTERVAL_SEC
    log.info(f"forecast_scanner start (interval={interval_sec}s, pairs={len(config.PAIRS)})")

    stop = {"flag": False}

    def _sig(_a, _b):
        stop["flag"] = True
        log.info("forecast_scanner: SIGTERM/SIGINT — stopping")

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop["flag"]:
        _heartbeat()
        try:
            scan_all_pairs()
        except Exception as e:
            log.exception(f"scan_all_pairs failed: {e}")
        _heartbeat()
        # дробим sleep чтобы быстрее реагировать на SIGTERM
        for _ in range(interval_sec):
            if stop["flag"]:
                break
            time.sleep(1)

    log.info("forecast_scanner exit")


if __name__ == "__main__":
    run_loop()
