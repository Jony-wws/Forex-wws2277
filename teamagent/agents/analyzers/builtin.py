"""14 analyzer-агентов. Каждый смотрит на разные аспекты ВСЕХ пар.

Все работают на реальных Yahoo-данных. Никаких заглушек.
"""
from __future__ import annotations
import json
import statistics
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from ... import config, indicators, volume_profile
from ...data import yahoo, news
from ..base import Agent


def _scan(pairs, fn, max_pairs: int = 28) -> list[dict]:
    """Helper: обходит список пар, применяет fn, скипает None."""
    out = []
    for p in pairs[:max_pairs]:
        try:
            r = fn(p)
            if r is not None:
                out.append(r)
        except Exception:
            continue
    return out


# ─────────────── analyzers ───────────────

class VWAPBiasAnalyzer(Agent):
    name = "analyzer_vwap_bias"
    category = "analyzer"
    interval_sec = 120

    def tick(self):
        def fn(p):
            df = yahoo.latest_bars(p, "5m", 200)
            if df.empty:
                return None
            ind = indicators.all_indicators(df)
            if not ind:
                return None
            bias = "BULL" if ind["close"] > ind["vwap"] else "BEAR"
            return {"pair": p, "bias": bias, "vwap": ind["vwap"], "close": ind["close"]}

        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class BBPRegimeAnalyzer(Agent):
    name = "analyzer_bbp_regime"
    category = "analyzer"
    interval_sec = 120

    def tick(self):
        def fn(p):
            df = yahoo.latest_bars(p, "1h", 100)
            if df.empty:
                return None
            ind = indicators.all_indicators(df)
            if not ind:
                return None
            regime = "BULL" if ind["bbp"] > 0 else "BEAR"
            return {"pair": p, "regime": regime, "bbp": ind["bbp"]}
        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class RSIDivergenceAnalyzer(Agent):
    name = "analyzer_rsi_divergence"
    category = "analyzer"
    interval_sec = 180

    def tick(self):
        def fn(p):
            df = yahoo.latest_bars(p, "1h", 80)
            if df.empty:
                return None
            close = df["Close"]
            rsi = indicators.rsi(close, 14)
            if len(close) < 30:
                return None
            # сравниваем последние 2 минимума/максимума
            highs_idx = (close == close.rolling(5, center=True).max())
            lows_idx = (close == close.rolling(5, center=True).min())
            recent_high_idx = close[highs_idx].tail(2).index
            recent_low_idx = close[lows_idx].tail(2).index

            div = "none"
            if len(recent_high_idx) >= 2:
                p1, p2 = close.loc[recent_high_idx[-2:]]
                r1, r2 = rsi.loc[recent_high_idx[-2:]]
                if p2 > p1 and r2 < r1:
                    div = "bearish"
            if len(recent_low_idx) >= 2:
                p1, p2 = close.loc[recent_low_idx[-2:]]
                r1, r2 = rsi.loc[recent_low_idx[-2:]]
                if p2 < p1 and r2 > r1:
                    div = "bullish"
            return {"pair": p, "divergence": div}
        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class TrendAlignmentAnalyzer(Agent):
    name = "analyzer_trend_alignment"
    category = "analyzer"
    interval_sec = 120

    def tick(self):
        def fn(p):
            df_4h = yahoo.latest_bars(p, "1h", 240)  # ≈10 дней «4H»
            df_1h = yahoo.latest_bars(p, "1h", 100)
            df_15 = yahoo.latest_bars(p, "15m", 100)
            if any(d.empty for d in (df_4h, df_1h, df_15)):
                return None
            i4 = indicators.all_indicators(df_4h)
            i1 = indicators.all_indicators(df_1h)
            i15 = indicators.all_indicators(df_15)
            if not (i4 and i1 and i15):
                return None
            bull = sum([
                i4["close"] > i4["ema50"],
                i1["close"] > i1["ema20"],
                i15["close"] > i15["ema20"],
            ])
            return {"pair": p, "tf_bull_count": int(bull)}
        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class BBSqueezeAnalyzer(Agent):
    name = "analyzer_bb_squeeze"
    category = "analyzer"
    interval_sec = 180

    def tick(self):
        def fn(p):
            df = yahoo.latest_bars(p, "1h", 100)
            if df.empty:
                return None
            close = df["Close"]
            ma = close.rolling(20).mean()
            sd = close.rolling(20).std()
            width = ((ma + 2*sd) - (ma - 2*sd))
            cur = float(width.iloc[-1])
            avg = float(width.rolling(50).mean().iloc[-1])
            if not (cur and avg):
                return None
            squeeze = cur / avg
            return {"pair": p, "squeeze_ratio": round(squeeze, 3)}
        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class MomentumBurstAnalyzer(Agent):
    name = "analyzer_momentum_burst"
    category = "analyzer"
    interval_sec = 90

    def tick(self):
        def fn(p):
            df = yahoo.latest_bars(p, "15m", 50)
            if df.empty:
                return None
            close = df["Close"]
            mom = float(indicators.momentum(close, 5).iloc[-1])
            return {"pair": p, "mom5_15m": round(mom, 4)}
        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class SessionStrengthAnalyzer(Agent):
    name = "analyzer_session_strength"
    category = "analyzer"
    interval_sec = 300

    def tick(self):
        hour_utc = datetime.now(timezone.utc).hour
        session = next((s for s, (a, b) in config.SESSIONS.items() if a <= hour_utc <= b), "Off")
        return {"session": session, "hour_utc": hour_utc}


class RangeBreakAnalyzer(Agent):
    name = "analyzer_range_break"
    category = "analyzer"
    interval_sec = 180

    def tick(self):
        def fn(p):
            df = yahoo.latest_bars(p, "15m", 100)
            if df.empty:
                return None
            highs = df["High"].rolling(20).max()
            lows = df["Low"].rolling(20).min()
            cur = float(df["Close"].iloc[-1])
            broke_up = cur > float(highs.iloc[-2])
            broke_down = cur < float(lows.iloc[-2])
            event = "up" if broke_up else "down" if broke_down else "none"
            return {"pair": p, "break": event}
        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class LiquiditySweepAnalyzer(Agent):
    name = "analyzer_liquidity_sweep"
    category = "analyzer"
    interval_sec = 180

    def tick(self):
        def fn(p):
            df = yahoo.latest_bars(p, "15m", 50)
            if df.empty or len(df) < 6:
                return None
            last = df.iloc[-1]
            prev_high = float(df["High"].iloc[-6:-1].max())
            prev_low = float(df["Low"].iloc[-6:-1].min())
            sweep = "none"
            if last["High"] > prev_high and last["Close"] < prev_high:
                sweep = "up_swept"
            elif last["Low"] < prev_low and last["Close"] > prev_low:
                sweep = "down_swept"
            return {"pair": p, "sweep": sweep}
        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class VolatilityRegimeAnalyzer(Agent):
    name = "analyzer_volatility_regime"
    category = "analyzer"
    interval_sec = 240

    def tick(self):
        def fn(p):
            df = yahoo.latest_bars(p, "1h", 200)
            if df.empty:
                return None
            atr = indicators.atr(df, 14)
            cur = float(atr.iloc[-1])
            avg = float(atr.rolling(50).mean().iloc[-1]) if len(atr) > 50 else cur
            ratio = cur / avg if avg > 0 else 1.0
            regime = "high" if ratio > 1.3 else "low" if ratio < 0.7 else "normal"
            return {"pair": p, "atr_ratio": round(ratio, 2), "regime": regime}
        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class MultiTFConsensusAnalyzer(Agent):
    name = "analyzer_multi_tf_consensus"
    category = "analyzer"
    interval_sec = 180

    def tick(self):
        # повторяет TrendAlignmentAnalyzer но даёт «consensus score»
        def fn(p):
            df_1h = yahoo.latest_bars(p, "1h", 100)
            df_15 = yahoo.latest_bars(p, "15m", 100)
            df_5 = yahoo.latest_bars(p, "5m", 100)
            if any(d.empty for d in (df_1h, df_15, df_5)):
                return None
            i1 = indicators.all_indicators(df_1h)
            i15 = indicators.all_indicators(df_15)
            i5 = indicators.all_indicators(df_5)
            if not (i1 and i15 and i5):
                return None
            bull_score = sum([
                i1["close"] > i1["ema20"],
                i15["close"] > i15["ema20"],
                i5["close"] > i5["ema20"],
            ])
            return {"pair": p, "bull_score": int(bull_score), "max": 3}
        rows = _scan(config.PAIRS, fn)
        return {"signals": rows, "count": len(rows)}


class VPAggregatorAnalyzer(Agent):
    name = "analyzer_vp_aggregator"
    category = "analyzer"
    interval_sec = 300

    def tick(self):
        # бежим VP только по 7 главным парам — иначе слишком долго
        majors = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"]
        def fn(p):
            vp = volume_profile.build(p)
            if vp.get("error"):
                return None
            return {
                "pair": p,
                "poc": vp["poc"],
                "vah": vp["vah"],
                "val": vp["val"],
                "direction": vp["direction"],
                "no_return": len(vp["forecast_to_utc5_midnight"]["no_return_levels"]),
            }
        rows = _scan(majors, fn, max_pairs=len(majors))
        return {"signals": rows, "count": len(rows)}


class NewsFilterAnalyzer(Agent):
    name = "analyzer_news_filter"
    category = "analyzer"
    interval_sec = 600

    def tick(self):
        out = []
        now = datetime.now(timezone.utc)
        for p in config.PAIRS:
            blocked = news.is_blackout(p, now)
            upcoming = news.upcoming_high_impact(p, hours_ahead=6)
            out.append({
                "pair": p,
                "blackout_now": bool(blocked),
                "upcoming_count": len(upcoming),
            })
        return {"signals": out, "count": len(out)}


class DXYAlignmentAnalyzer(Agent):
    name = "analyzer_dxy_alignment"
    category = "analyzer"
    interval_sec = 600

    def tick(self):
        # DXY = USD index — если он выше EMA20, USD сильный → SELL EURUSD/GBPUSD/AUDUSD/NZDUSD
        # Yahoo тикер DX-Y.NYB
        try:
            import yfinance as yf
            df = yf.download("DX-Y.NYB", interval="1h", period="5d", progress=False, threads=False)
            if df is None or df.empty:
                return {"dxy": None}
            close = df["Close"].iloc[-1]
            ema = df["Close"].ewm(span=20).mean().iloc[-1]
            close_v = float(close.item() if hasattr(close, "item") else close)
            ema_v = float(ema.item() if hasattr(ema, "item") else ema)
            return {"dxy_close": close_v, "dxy_ema20": ema_v, "usd_strong": close_v > ema_v}
        except Exception as e:
            return {"error": str(e)}


class FundamentalMacroAnalyzer(Agent):
    """Sources rate-differential / 10y-yield-differential / CPI YoY per pair from
    FRED (free, no API key, 24h cache). Computes a per-pair macro tilt score
    (BUY/SELL/NEUTRAL with confidence). The forecast scanner reads this same
    fundamentals.json directly via teamagent.fundamentals — this agent's role is
    to (a) refresh the cache periodically and (b) surface the snapshot on the
    dashboard.

    Why this is honest "thinking like institutionals" (per user request):
    - Real macro flows track interest rate differentials, bond yields, CPI
      surprises — exactly what this agent reads from FRED public CSVs.
    - The data is the SAME data the Fed/ECB/BoE publish; nothing is simulated.
    - We do NOT have private order flow / paid news sentiment — that would
      require paid feeds; this is the realistic free-tier ceiling.
    """
    name = "analyzer_fundamental_macro"
    category = "analyzer"
    interval_sec = 6 * 60 * 60   # refresh every 6h (FRED data updates daily)

    def tick(self):
        try:
            from ... import fundamentals
            data = fundamentals.get_cached(force_refresh=False)
            tilts = fundamentals.all_pair_tilts()
            ccy_view = {
                ccy: {
                    "policy_rate": (vals.get("policy_rate") or {}).get("value"),
                    "10y_yield":   (vals.get("10y_yield")   or {}).get("value"),
                    "cpi_yoy_pct": (vals.get("cpi")         or {}).get("yoy_pct"),
                }
                for ccy, vals in data.get("currencies", {}).items()
            }
            # top biases (highest |tilt_score|)
            ranked = sorted(
                tilts.get("tilts", {}).items(),
                key=lambda kv: abs(kv[1].get("tilt_score", 0)),
                reverse=True,
            )[:10]
            top = [
                {
                    "pair": p,
                    "side": v.get("side"),
                    "tilt_score": v.get("tilt_score"),
                    "confidence_pct": v.get("confidence_pct"),
                }
                for p, v in ranked
            ]
            return {
                "currencies": ccy_view,
                "n_pairs_with_tilt": sum(
                    1 for v in tilts.get("tilts", {}).values()
                    if v.get("side") in ("BUY", "SELL")
                ),
                "top_bias_pairs": top,
                "fundamentals_as_of": data.get("as_of"),
                "source": data.get("source"),
            }
        except Exception as e:
            return {"error": str(e)}
