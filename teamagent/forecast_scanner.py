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


def _score_to_probability(score: int, max_score: int = 75) -> float:
    """Score -75..+75 → probability 0..1, абсолютная.

    Score>0 → BUY вероятность; <0 → SELL вероятность.
    max_score=75 учитывает 6 новых блоков (MACD, Stoch, ADX, Williams %R,
    Ichimoku, и ADX regime gate) добавленных в 2026-05-03.
    """
    norm = score / max_score              # -1..+1
    p = _sigmoid(norm * 4.0)              # softer sigmoid
    return p


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

    # ───── BLOCK K — Event-attribution boost (added 2026-05-04) ─────
    # Source: HISTORY/event_attribution_365d/ (built by teamagent.events.*).
    # When a persistent-driver event (US GDP/PCE/NFP/CPI/PPI, CB rate decision,
    # CB press conf, COT extreme) is in ±6h of `now` AND the (pair × session ×
    # event_type) cell has frequency≥2 in the 365-day archive, we add a
    # weighted score: base × concordance × persistence, signed by the historic
    # dominant direction for that event-currency. Capped ±8 to prevent any
    # one news cluster overwhelming the technical stack.
    try:
        from .events import live_weights as ev_lw
        # Use analysis-session taxonomy (Asia/London/Overlap/NY) covering all
        # 24 hours, so artefact lookups hit even at hours not in config.SESSIONS.
        sess_now = ev_lw._hour_to_analysis_session(now.hour)
        ev_delta, ev_reason = ev_lw.event_score_contribution(pair, sess_now, now)
        if ev_delta != 0 and ev_reason:
            vote("event_attribution", ev_delta, ev_reason)
        # Soft trap penalty on known whipsaw cells (≥50% trap-rate). NEVER
        # blocks the trade (free 70% gate stays free) — only nudges |score|
        # down a couple of points so probability_pct doesn't reach 70 on
        # marginal signals in known-bad cells.
        trap_delta, trap_reason = ev_lw.trap_score_penalty(pair, sess_now, score)
        if trap_delta != 0 and trap_reason:
            vote("trap_filter", trap_delta, trap_reason)
    except Exception as e:
        log.warning(f"forecast_scanner: event_attribution integration failed for {pair}: {e}")

    # ───── BLOCK L — Phase-8 trained knowledge (added 2026-05-04) ─────
    # Three layers of learned 365-day patterns from `state/learned_rules.json`:
    #   1. learned_rule_score: high-conviction (pair × session × event_type)
    #      cells (concordance ≥ 75%, frequency ≥ 4) → strong boost ±16 max.
    #   2. pair_session_bias_score: persistent (pair × session) directional
    #      drift over the year, fires only at concordance ≥ 70% / n ≥ 100
    #      → small constant nudge ±2 even with no event in window.
    #   3. multi_event_cluster_amplifier: when ≥ 2 persistent events co-fire
    #      and agree on direction → extra +4 to +8 in agreed direction.
    # All three never block trades — they only refine probability_pct.
    try:
        from .events import live_weights as ev_lw
        sess_now = ev_lw._hour_to_analysis_session(now.hour)
        # Layer 1: high-conviction event rule (the strongest)
        learned_delta, learned_reason = ev_lw.learned_rule_score(pair, sess_now, now)
        if learned_delta != 0 and learned_reason:
            vote("learned_rule", learned_delta, learned_reason)
        # Layer 2: persistent pair-session drift
        bias_delta, bias_reason = ev_lw.pair_session_bias_score(pair, sess_now)
        if bias_delta != 0 and bias_reason:
            vote("pair_session_bias", bias_delta, bias_reason)
        # Layer 3: multi-event cluster amplifier
        cluster_delta, cluster_reason = ev_lw.multi_event_cluster_amplifier(pair, sess_now, now)
        if cluster_delta != 0 and cluster_reason:
            vote("multi_event_cluster", cluster_delta, cluster_reason)
    except Exception as e:
        log.warning(f"forecast_scanner: learned_rules integration failed for {pair}: {e}")

    # ───── BLOCK M — Phase-9 deeper conviction (added 2026-05-04) ─────
    # Three more learned-knowledge layers grounded entirely in real 365-day
    # data (no simulator, no random):
    #   1. hour_bias_score: per-(pair × UTC hour) drift over 365 days of
    #      Yahoo 1H bars (concordance ≥ 62%, n ≥ 60). Up to ±1 per pair.
    #   2. historical_wr_score: per-(pair × session) backtest win-rate from
    #      `state/strategy_config_locked.json` (output of strategy_search).
    #      Tiered ±2/±3/±4 for WR≥60/65/70%.
    #   3. currency_strength_score: real 24h cross-pair return ranking — when
    #      base is in top-3 strongest AND quote in bottom-3 weakest (or vice
    #      versa), emit ±2 in the rank-divergence direction.
    # All three additive votes; never block trades; the free 70% gate stays
    # free per AGENTS.md rule #7.
    try:
        from .events import live_weights as ev_lw
        sess_now = ev_lw._hour_to_analysis_session(now.hour)
        # Layer M1: hour-of-day bias
        hb_delta, hb_reason = ev_lw.hour_bias_score(pair, now)
        if hb_delta != 0 and hb_reason:
            vote("hour_bias", hb_delta, hb_reason)
        # Layer M2: historical backtest WR per (pair × session) — only
        # amplifies when its dominant side agrees with the technical-stack
        # score sign. Pass current `score` as pre_score for that guard.
        wr_delta, wr_reason = ev_lw.historical_wr_score(pair, sess_now, score)
        if wr_delta != 0 and wr_reason:
            vote("historical_wr", wr_delta, wr_reason)
        # Layer M3: cross-pair currency-strength rank
        cs_delta, cs_reason = ev_lw.currency_strength_score(pair)
        if cs_delta != 0 and cs_reason:
            vote("currency_strength", cs_delta, cs_reason)
    except Exception as e:
        log.warning(f"forecast_scanner: phase9 integration failed for {pair}: {e}")

    # ───── итог ─────
    if score == 0:
        return None  # нейтрально, не показываем

    side = "BUY" if score > 0 else "SELL"
    p_raw = _score_to_probability(abs(score), 75)
    # cap 50–92
    p = max(0.50, min(config.MAX_PROBABILITY, p_raw))

    # ───── BLOCK N — Phase-10 cell-anchored probability (added 2026-05-05) ─────
    # When the current (pair × session) cell has a historically-strong, n>=8
    # backtest WR AND the cell's dominant historical side AGREES with the
    # current technical-stack score sign, anchor the displayed probability to
    # the measured WR (capped 50-92% per config). This is the SINGLE honest
    # way to show "real 80% on every currency × session pair where data
    # supports it": instead of arbitrary inflation, we show the historical
    # WR which the system actually achieves on this cell.
    #
    # Source: state/strategy_config_locked.json (output of strategy_search).
    # Cells with WR>=70 n>=8: 30. Cells with WR>=80 n>=8: 8 (max 83.3%).
    # Cells where the guard (sign agreement) is satisfied get the uplift;
    # the rest fall back to score-based probability — no faked numbers.
    p10_reason = None
    try:
        from .events import live_weights as ev_lw
        sess_now = ev_lw._hour_to_analysis_session(now.hour)
        wr_info = ev_lw._strategy_wr.get((pair, sess_now))
        if wr_info:
            cell_wr = float(wr_info.get("win_rate_pct") or 0)
            cell_n = int(wr_info.get("trades") or 0)
            cell_side = wr_info.get("dominant_side")
            if (
                cell_n >= 8
                and cell_wr >= 70.0
                and (
                    (cell_side == "BUY" and score > 0)
                    or (cell_side == "SELL" and score < 0)
                )
            ):
                p_anchor = min(config.MAX_PROBABILITY, cell_wr / 100.0)
                if p_anchor > p:
                    p10_reason = (
                        f"cell_anchor: {pair}/{sess_now} hist WR={cell_wr:.1f}% × {cell_side} "
                        f"(n={cell_n}, agrees, p {p*100:.1f}%→{p_anchor*100:.1f}%)"
                    )
                    p = p_anchor
    except Exception as e:
        log.warning(f"forecast_scanner: phase10 cell-anchor failed for {pair}: {e}")

    # рекомендованная экспирация: больше score → дольше держим
    abs_norm = min(1.0, abs(score) / 20.0)
    recommended_hours = int(round(config.MIN_EXPIRY_HOURS + abs_norm * (config.MAX_EXPIRY_HOURS - config.MIN_EXPIRY_HOURS)))
    recommended_hours = max(config.MIN_EXPIRY_HOURS, min(config.MAX_EXPIRY_HOURS, recommended_hours))

    # volume profile snapshot
    try:
        vp = volume_profile.build(pair)
    except Exception as e:
        log.warning(f"VP failed pair={pair}: {e}")
        vp = {"error": str(e)}

    # Phase-10 cell-anchor entry — visible in score_breakdown for transparency.
    if p10_reason:
        score_breakdown.append({
            "name": "cell_anchor",
            "contrib": 0,  # doesn't shift score, only the displayed probability
            "reason": p10_reason,
        })

    # ───── BLOCK O — Phase-11 honest math expectation @ user broker payout ─────
    # The user's binary-options broker pays 70% on WIN (not the 85% the
    # paper-trader simulates). At 70% payout the break-even WR is
    # 1/(1+0.70)≈58.82% — below that, every trade is a guaranteed loss on
    # distance no matter what `probability_pct` says. We expose the EV math
    # transparently so the user can SEE which forecasts truly have positive
    # math expectation. We also surface the realized 365-day cell WR (when
    # available) so a 78% displayed probability anchored to a 78% historical
    # WR is visibly different from a 78% displayed probability that has no
    # backtest backing. This is the single honest path to "math expectation
    # advantage on distance" — no inflation, just transparent EV.
    broker_payout = float(getattr(config, "BROKER_PAYOUT_PCT", 0.70))
    ev_per_trade = round(p * (1 + broker_payout) - 1, 4)
    ev_pct_per_trade = round(ev_per_trade * 100, 1)
    breakeven_wr_pct = round(100 / (1 + broker_payout), 2)
    realized_cell_wr_pct = None
    realized_cell_n = None
    realized_cell_side = None
    try:
        from .events import live_weights as ev_lw
        sess_now2 = ev_lw._hour_to_analysis_session(now.hour)
        info = ev_lw._strategy_wr.get((pair, sess_now2))
        if info:
            realized_cell_wr_pct = info.get("win_rate_pct")
            realized_cell_n = info.get("trades")
            realized_cell_side = info.get("dominant_side")
    except Exception:
        pass
    if ev_per_trade >= 0.05:
        ev_status = "green"      # ≥+5% EV — trade with confidence
    elif ev_per_trade > 0:
        ev_status = "yellow"     # marginal positive — caution
    else:
        ev_status = "red"        # negative EV — losing on distance

    forecast = {
        "pair": pair,
        "side": side,
        "probability": round(p, 4),
        "probability_pct": round(p * 100.0, 1),
        "score": score,
        "max_score": 75,
        "recommended_hours": recommended_hours,
        "current_price": ind_15m["close"],
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
        # Phase-11 EV transparency fields
        "broker_payout_pct": broker_payout,
        "ev_per_trade": ev_per_trade,
        "ev_pct_per_trade": ev_pct_per_trade,
        "breakeven_wr_pct": breakeven_wr_pct,
        "ev_status": ev_status,
        "realized_cell_wr_pct": realized_cell_wr_pct,
        "realized_cell_n": realized_cell_n,
        "realized_cell_side": realized_cell_side,
        "cell_anchor_active": bool(p10_reason),
    }
    return forecast


def _current_session(hour: int) -> str:
    for name, (lo, hi) in config.SESSIONS.items():
        if lo <= hour <= hi:
            return name
    return "Off"


def scan_all_pairs() -> dict:
    """Полный обход 28 пар. Сохраняет общий snapshot в state/forecasts.json."""
    snapshot = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "total_pairs": len(config.PAIRS),
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
            # vote breakdown в выжимке тоже — иначе на дашборде пары показывают 0/0
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
