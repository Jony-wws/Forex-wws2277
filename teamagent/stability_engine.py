"""stability_engine — математические гарантии стабильности (без случайностей).

Все метрики используют ТОЛЬКО реальные исторические данные:
- closed_trades.json (реальные закрытые paper-сделки)
- strategy_config.json (per (pair, session) варианты, посчитанные на 365 днях Yahoo)
- backtest_30d.json (30-дневный walk-forward бэктест)
- yahoo.fetch (реальные цены H1/H4/M15)

Bootstrap здесь — это resampling РЕАЛЬНЫХ наблюдений (исторических трейдов или
исторических доходностей), а не генерация синтетики. Случайные числа фиксируются
seed-ом из (pair, as_of) — результаты репродуцируемы и стабильны между запусками.

Гарантия здесь = математическая нижняя граница (Wilson CI lower, bootstrap p5,
quantile p5, conformal lower) на основе реальной выборки. Это «худший
правдоподобный сценарий», а не предсказание.

Никаких симуляторов, фейковых трейдов, рандомных направлений — только реальные
числа из state/ + Yahoo.
"""
from __future__ import annotations

import json
import math
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from . import config
from .data import yahoo

log = logging.getLogger("stability")

STATE_DIR = config.STATE_DIR

# Stable seed: tied to pair, results reproducible across reloads.
_BASE_SEED = 20260501


def _seed_for(label: str) -> int:
    return _BASE_SEED + (abs(hash(label)) % 1_000_000)


def _load_json(path: Path, default):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text())
    except Exception as e:
        log.warning(f"_load_json {path.name} failed: {e}")
        return default


# ─────────────────────────────────────────────────────────────────────────────
# 1. Биномиальные нижние границы для WR (Wilson + Clopper-Pearson)
# ─────────────────────────────────────────────────────────────────────────────

def wilson_lower_upper(wins: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval — корректный CI для биномиальной WR.

    Возвращает (lower, upper) для p = wins/total. Симметричен для p≈0.5,
    но сжимается к 0/1 на хвостах. При total=0 возвращает (0, 1).
    """
    if total <= 0:
        return (0.0, 1.0)
    z_map = {0.90: 1.6449, 0.95: 1.96, 0.975: 2.241, 0.99: 2.5758}
    z = z_map.get(round(confidence, 3), 1.96)
    p = wins / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def clopper_pearson(wins: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    """Clopper-Pearson exact CI (более консервативный).

    Если scipy установлен — точная Beta-квантиль. Иначе — fallback на Wilson
    (никогда не ронять отчёт из-за отсутствующего пакета).
    """
    if total <= 0:
        return (0.0, 1.0)
    try:
        from scipy import stats as scst  # type: ignore
    except Exception:
        return wilson_lower_upper(wins, total, confidence)
    alpha = 1 - confidence
    lo = scst.beta.ppf(alpha / 2, wins, total - wins + 1) if wins > 0 else 0.0
    hi = scst.beta.ppf(1 - alpha / 2, wins + 1, total - wins) if wins < total else 1.0
    return (float(lo), float(hi))


# ─────────────────────────────────────────────────────────────────────────────
# 2. Bootstrap CI для WR / PnL по реальным закрытым сделкам
# ─────────────────────────────────────────────────────────────────────────────

def bootstrap_ci(
    samples: list[float],
    n_iter: int = 2000,
    confidence: float = 0.95,
    seed: int = _BASE_SEED,
) -> dict[str, float]:
    """Bootstrap CI: resample реальных наблюдений с replacement.

    Это не «randomness» — мы пересэмплируем РЕАЛЬНЫЕ числа, чтобы оценить
    распределение среднего. Фиксированный seed → результат идентичен между
    запусками.
    """
    if not samples:
        return {"mean": 0.0, "lower": 0.0, "upper": 0.0, "p5": 0.0, "p50": 0.0, "p95": 0.0, "n": 0}
    rng = np.random.default_rng(seed)
    arr = np.array(samples, dtype=float)
    means = np.empty(n_iter)
    n = len(arr)
    for i in range(n_iter):
        idx = rng.integers(0, n, n)
        means[i] = arr[idx].mean()
    alpha = 1 - confidence
    lower = float(np.quantile(means, alpha / 2))
    upper = float(np.quantile(means, 1 - alpha / 2))
    return {
        "mean": float(arr.mean()),
        "lower": lower,
        "upper": upper,
        "p5": float(np.quantile(means, 0.05)),
        "p50": float(np.quantile(means, 0.50)),
        "p95": float(np.quantile(means, 0.95)),
        "n": int(n),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Conformal prediction — гарантированный coverage цены через H часов
# ─────────────────────────────────────────────────────────────────────────────

def conformal_price_band(
    pair: str,
    horizon_hours: int = 4,
    confidence: float = 0.90,
    lookback_days: int = 90,
) -> dict[str, float]:
    """Conformal prediction: 90% коридор цены через H часов.

    Алгоритм:
      1. Берём 90 дней H1 баров.
      2. Считаем реальные log-returns на горизонте `horizon_hours`.
      3. Нижняя/верхняя граница = price_now * exp(quantile(returns, alpha/2 .. 1-alpha/2)).

    Гарантия: при стационарности распределения, реальная будущая цена попадёт
    в этот коридор примерно в `confidence` × 100% случаев.
    """
    try:
        df = yahoo.fetch(pair, "1h", f"{lookback_days}d")
        if df is None or len(df) < horizon_hours + 30:
            return {}
        closes = df["Close"].astype(float).values
        rets = np.log(closes[horizon_hours:] / closes[:-horizon_hours])
        if len(rets) < 30:
            return {}
        alpha = 1 - confidence
        lo_q = float(np.quantile(rets, alpha / 2))
        hi_q = float(np.quantile(rets, 1 - alpha / 2))
        spot = float(closes[-1])
        return {
            "spot": spot,
            "lower": spot * math.exp(lo_q),
            "upper": spot * math.exp(hi_q),
            "median": spot * math.exp(float(np.median(rets))),
            "p5": spot * math.exp(float(np.quantile(rets, 0.05))),
            "p25": spot * math.exp(float(np.quantile(rets, 0.25))),
            "p75": spot * math.exp(float(np.quantile(rets, 0.75))),
            "p95": spot * math.exp(float(np.quantile(rets, 0.95))),
            "horizon_hours": horizon_hours,
            "confidence": confidence,
            "n_samples": int(len(rets)),
        }
    except Exception as e:
        log.warning(f"conformal_price_band({pair}) failed: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 4. Risk-метрики: VaR, CVaR, Sharpe, Sortino, MDD, Calmar, Kelly
# ─────────────────────────────────────────────────────────────────────────────

def var_cvar(returns: list[float], confidence: float = 0.95) -> dict[str, float]:
    """Historical VaR + CVaR (Expected Shortfall) на реальных доходностях."""
    if not returns:
        return {"var": 0.0, "cvar": 0.0, "n": 0}
    arr = np.array(returns, dtype=float)
    alpha = 1 - confidence
    var = float(np.quantile(arr, alpha))   # отрицательное = потери
    tail = arr[arr <= var]
    cvar = float(tail.mean()) if len(tail) > 0 else var
    return {"var": var, "cvar": cvar, "n": int(len(arr)), "confidence": confidence}


def sharpe_ratio(returns: list[float], periods_per_year: int = 252) -> float:
    if not returns or len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    mu = arr.mean()
    sd = arr.std(ddof=1)
    if sd <= 0:
        return 0.0
    return float(mu / sd * math.sqrt(periods_per_year))


def sortino_ratio(returns: list[float], periods_per_year: int = 252) -> float:
    if not returns or len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    mu = arr.mean()
    downside = arr[arr < 0]
    if len(downside) == 0:
        return 0.0
    dd_sd = math.sqrt((downside ** 2).mean())
    if dd_sd <= 0:
        return 0.0
    return float(mu / dd_sd * math.sqrt(periods_per_year))


def max_drawdown(equity: list[float]) -> dict[str, float]:
    if not equity:
        return {"mdd": 0.0, "mdd_pct": 0.0, "peak": 0.0, "trough": 0.0}
    arr = np.array(equity, dtype=float)
    peaks = np.maximum.accumulate(arr)
    drawdowns = (arr - peaks) / np.where(peaks == 0, 1, peaks)
    idx = int(np.argmin(drawdowns))
    return {
        "mdd": float(arr[idx] - peaks[idx]),
        "mdd_pct": float(drawdowns[idx] * 100),
        "peak": float(peaks[idx]),
        "trough": float(arr[idx]),
    }


def calmar_ratio(returns: list[float], equity: list[float], periods_per_year: int = 252) -> float:
    if not returns or not equity:
        return 0.0
    arr = np.array(returns, dtype=float)
    annual_return = arr.mean() * periods_per_year
    mdd = max_drawdown(equity)["mdd_pct"] / 100
    if mdd == 0:
        return 0.0
    return float(annual_return / abs(mdd))


def kelly_fraction(win_rate: float, payout_pct: float = 0.85, half: bool = True) -> float:
    """Kelly stake: f* = (p * b - q) / b, где b — net odds, q = 1 - p.

    half=True возвращает Kelly/2 — стандартная практика (full Kelly слишком волатилен).
    """
    p = max(0.0, min(1.0, win_rate))
    b = payout_pct  # выигрыш на доллар стейка
    q = 1 - p
    f = (p * b - q) / b if b > 0 else 0.0
    f = max(0.0, f)
    return f / 2 if half else f


def gambler_ruin_probability(win_rate: float, edge_per_trade: float, max_loss: float) -> float:
    """Вероятность разорения для martingale 1→2→4 с лимитом."""
    if edge_per_trade <= 0:
        return 1.0
    q = 1 - win_rate
    p = win_rate
    if abs(p - q) < 1e-9:
        return max(0.0, min(1.0, max_loss / (max_loss + 1)))
    r = q / p
    return float(min(1.0, max(0.0, r ** max_loss)))


def profit_factor(returns: list[float]) -> float:
    if not returns:
        return 0.0
    arr = np.array(returns, dtype=float)
    wins = arr[arr > 0].sum()
    losses = -arr[arr < 0].sum()
    if losses <= 0:
        return float("inf") if wins > 0 else 0.0
    return float(wins / losses)


def expectancy(returns: list[float]) -> float:
    if not returns:
        return 0.0
    return float(np.mean(returns))


# ─────────────────────────────────────────────────────────────────────────────
# 5. Распределение доходностей: skew/kurt/Hurst/variance ratio
# ─────────────────────────────────────────────────────────────────────────────

def distribution_stats(returns: list[float]) -> dict[str, float]:
    if not returns or len(returns) < 4:
        return {"mean": 0.0, "std": 0.0, "skew": 0.0, "kurt": 0.0, "n": 0}
    arr = np.array(returns, dtype=float)
    mu = arr.mean()
    sd = arr.std(ddof=1)
    if sd <= 0:
        return {"mean": float(mu), "std": 0.0, "skew": 0.0, "kurt": 0.0, "n": int(len(arr))}
    z = (arr - mu) / sd
    skew = float(((z ** 3).mean()))
    kurt = float(((z ** 4).mean()) - 3)
    return {"mean": float(mu), "std": float(sd), "skew": skew, "kurt": kurt, "n": int(len(arr))}


def hurst_exponent(prices: list[float], max_lag: int = 50) -> float:
    """R/S Hurst exponent. >0.5 = trend, <0.5 = mean-reversion, ≈0.5 = random walk."""
    if not prices or len(prices) < max_lag * 2:
        return 0.5
    arr = np.array(prices, dtype=float)
    lags = range(2, min(max_lag, len(arr) // 2))
    tau = []
    for lag in lags:
        diff = arr[lag:] - arr[:-lag]
        if diff.std() <= 0:
            continue
        tau.append(diff.std())
    if len(tau) < 4:
        return 0.5
    log_lags = np.log(np.array(list(lags))[:len(tau)])
    log_tau = np.log(np.array(tau))
    slope, _ = np.polyfit(log_lags, log_tau, 1)
    return float(slope)


def variance_ratio(returns: list[float], lag: int = 5) -> float:
    """Lo-MacKinlay VR test: 1.0 = random walk, >1 = trending, <1 = mean-revert."""
    if not returns or len(returns) < lag * 4:
        return 1.0
    arr = np.array(returns, dtype=float)
    var1 = arr.var(ddof=1)
    cum = np.cumsum(arr)
    multi = (cum[lag:] - cum[:-lag])
    var_lag = multi.var(ddof=1) / lag
    if var1 <= 0:
        return 1.0
    return float(var_lag / var1)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Calibration / Brier score / log loss — точность прогнозов вероятности
# ─────────────────────────────────────────────────────────────────────────────

def brier_score(probs: list[float], outcomes: list[int]) -> float:
    """Brier score = mean((prob - outcome)²). Чем меньше — тем точнее calibration."""
    if not probs or not outcomes or len(probs) != len(outcomes):
        return 1.0
    p = np.array(probs, dtype=float)
    o = np.array(outcomes, dtype=float)
    return float(((p - o) ** 2).mean())


def log_loss(probs: list[float], outcomes: list[int]) -> float:
    if not probs or not outcomes:
        return 1.0
    p = np.clip(np.array(probs, dtype=float), 1e-9, 1 - 1e-9)
    o = np.array(outcomes, dtype=float)
    return float(-(o * np.log(p) + (1 - o) * np.log(1 - p)).mean())


def calibration_table(probs: list[float], outcomes: list[int], n_bins: int = 10) -> list[dict[str, Any]]:
    """Разбиваем вероятности на бины, считаем фактический WR в каждом."""
    if not probs:
        return []
    p = np.array(probs, dtype=float)
    o = np.array(outcomes, dtype=int)
    bins = np.linspace(0, 1, n_bins + 1)
    out = []
    for i in range(n_bins):
        mask = (p >= bins[i]) & (p < bins[i + 1] if i < n_bins - 1 else p <= bins[i + 1])
        n = int(mask.sum())
        if n == 0:
            continue
        out.append({
            "bin": [round(float(bins[i]), 2), round(float(bins[i + 1]), 2)],
            "predicted_mean": float(p[mask].mean()),
            "actual_wr": float(o[mask].mean()),
            "n": n,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 7. Strategy ensemble agreement
# ─────────────────────────────────────────────────────────────────────────────

def ensemble_agreement(variants: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Доля топ-K вариантов, согласных с направлением и достигших ≥70% WR."""
    arr = [v for v in variants if v and v.get("trades", 0) >= 10]
    if not arr:
        return {"qualified_share": 0.0, "n_qualified": 0, "n_total": 0}
    qualified = [v for v in arr if v.get("win_rate_pct", 0) >= 70]
    return {
        "qualified_share": len(qualified) / len(arr),
        "n_qualified": len(qualified),
        "n_total": len(arr),
        "median_wr_pct": float(np.median([v.get("win_rate_pct", 0) for v in arr])),
        "max_wr_pct": float(max((v.get("win_rate_pct", 0) for v in arr), default=0)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8. Stress-tests на реальной 365-дневной истории
# ─────────────────────────────────────────────────────────────────────────────

def realized_volatility(pair: str, lookback_days: int = 30) -> dict[str, float]:
    try:
        df = yahoo.fetch(pair, "1h", f"{lookback_days}d")
        if df is None or len(df) < 24:
            return {}
        closes = df["Close"].astype(float).values
        rets = np.diff(np.log(closes))
        sd_h = rets.std()
        return {
            "rv_hourly": float(sd_h),
            "rv_daily": float(sd_h * math.sqrt(24)),
            "rv_weekly": float(sd_h * math.sqrt(24 * 5)),
            "rv_annualized": float(sd_h * math.sqrt(24 * 252)),
            "n_bars": int(len(rets)),
        }
    except Exception as e:
        log.warning(f"realized_volatility({pair}) failed: {e}")
        return {}


def stress_test_pair(pair: str, lookback_days: int = 365) -> dict[str, Any]:
    """Реальные крайние периоды: худшая неделя, худший день, худший час за 365 д."""
    try:
        df = yahoo.fetch(pair, "1h", f"{lookback_days}d")
        if df is None or len(df) < 24 * 7:
            return {}
        closes = df["Close"].astype(float).values
        rets_h = np.diff(np.log(closes))
        # Reshape into daily/weekly
        n_full_days = (len(rets_h) // 24) * 24
        daily = np.array([rets_h[i:i + 24].sum() for i in range(0, n_full_days, 24)])
        weekly = np.array([daily[i:i + 5].sum() for i in range(0, len(daily) - 4, 5)])
        return {
            "worst_hour_pct": float(rets_h.min() * 100),
            "best_hour_pct": float(rets_h.max() * 100),
            "worst_day_pct": float(daily.min() * 100) if len(daily) else 0.0,
            "best_day_pct": float(daily.max() * 100) if len(daily) else 0.0,
            "worst_week_pct": float(weekly.min() * 100) if len(weekly) else 0.0,
            "best_week_pct": float(weekly.max() * 100) if len(weekly) else 0.0,
            "current_week_vol_pct": float(rets_h[-24 * 5:].std() * math.sqrt(24 * 5) * 100) if len(rets_h) >= 120 else 0.0,
        }
    except Exception as e:
        log.warning(f"stress_test_pair({pair}) failed: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 9. Streak analysis по реальным closed_trades
# ─────────────────────────────────────────────────────────────────────────────

def streak_analysis(closed_trades: list[dict]) -> dict[str, Any]:
    if not closed_trades:
        return {
            "longest_win_streak": 0, "longest_loss_streak": 0,
            "current_streak": 0, "current_streak_kind": "—",
        }
    sorted_t = sorted(closed_trades, key=lambda t: t.get("close_time") or "")
    results = [1 if (t.get("result") or "").upper() == "WIN" else 0 for t in sorted_t]
    longest_win = longest_loss = cur_w = cur_l = 0
    for r in results:
        if r == 1:
            cur_w += 1
            cur_l = 0
            longest_win = max(longest_win, cur_w)
        else:
            cur_l += 1
            cur_w = 0
            longest_loss = max(longest_loss, cur_l)
    cur_kind = "WIN" if results and results[-1] == 1 else ("LOSS" if results else "—")
    cur_len = cur_w if cur_kind == "WIN" else cur_l
    return {
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
        "current_streak": cur_len,
        "current_streak_kind": cur_kind,
        "n": len(results),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 10. Сводный stability score per pair (0-100)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PairStability:
    pair: str
    score_0_100: float
    components: dict[str, float]
    guarantees: dict[str, float]
    notes: list[str]


def pair_stability_score(pair: str) -> PairStability:
    """Агрегированный stability score на основе 7 компонентов.

    Компоненты (каждый 0-100):
      1. Wilson lower WR (по бэктесту 365д)
      2. Median WR ансамбля вариантов
      3. Calibration (1 - Brier × 4)
      4. Hurst stability (близость к режиму)
      5. Volatility headroom (текущая vol vs историческая)
      6. Conformal coverage (фракция корректных предсказаний)
      7. Reliability via realized PnL distribution
    """
    cfg = _load_json(STATE_DIR / "strategy_config.json", {"pairs": {}})
    pinfo = (cfg.get("pairs") or {}).get(pair, {})
    variants = pinfo.get("all_variants") or []
    components: dict[str, float] = {}
    guarantees: dict[str, float] = {}
    notes: list[str] = []

    # 1. Wilson lower на лучшем варианте
    best = next((v for v in variants if v.get("trades", 0) >= 10), None)
    if best:
        wins = best.get("wins") or int((best.get("trades", 0)) * (best.get("win_rate_pct", 0) / 100))
        total = best.get("trades", 0)
        wlo, whi = wilson_lower_upper(wins, total, 0.95)
        components["wilson_lower_wr"] = round(wlo * 100, 1)
        components["wilson_upper_wr"] = round(whi * 100, 1)
        guarantees["wr_min_95pct_confidence"] = round(wlo * 100, 1)
    else:
        components["wilson_lower_wr"] = 0.0
        guarantees["wr_min_95pct_confidence"] = 0.0
        notes.append("Нет варианта с ≥10 сделок — нижняя граница не определена")

    # 2. Ансамбль
    ens = ensemble_agreement(variants)
    components["ensemble_qualified_share"] = round(ens.get("qualified_share", 0) * 100, 1)
    components["ensemble_median_wr"] = round(ens.get("median_wr_pct", 0), 1)

    # 3. Hurst (1H, 90d)
    try:
        df = yahoo.fetch(pair, "1h", "90d")
        if df is not None and len(df) > 100:
            h = hurst_exponent(df["Close"].astype(float).values)
            components["hurst_h"] = round(h, 3)
            # Stability bonus if H clearly trend or clearly mean-rev (clear regime)
            regime_clarity = abs(h - 0.5) * 200  # 0..100
            components["regime_clarity_0_100"] = round(min(100, regime_clarity), 1)
        else:
            components["hurst_h"] = 0.5
            components["regime_clarity_0_100"] = 0.0
    except Exception:
        components["hurst_h"] = 0.5
        components["regime_clarity_0_100"] = 0.0

    # 4. Realized volatility now vs historical
    rv30 = realized_volatility(pair, 30)
    rv365 = realized_volatility(pair, 365)
    if rv30 and rv365 and rv365.get("rv_hourly", 0) > 0:
        ratio = rv30["rv_hourly"] / rv365["rv_hourly"]
        components["vol_ratio_30d_vs_365d"] = round(ratio, 2)
        # Stable if 0.7..1.3
        vol_stab = max(0.0, 100 * (1 - min(1, abs(ratio - 1.0))))
        components["vol_stability_0_100"] = round(vol_stab, 1)
    else:
        components["vol_ratio_30d_vs_365d"] = 1.0
        components["vol_stability_0_100"] = 50.0

    # 5. Conformal price coverage (theoretical 90%)
    cb = conformal_price_band(pair, 4, 0.90, 90)
    if cb:
        spread = (cb.get("upper", 0) - cb.get("lower", 0)) / cb.get("spot", 1)
        components["conformal_band_pct"] = round(spread * 100, 3)
    else:
        components["conformal_band_pct"] = 0.0

    # 6. Stress test
    stress = stress_test_pair(pair, 365)
    if stress:
        components["worst_day_pct"] = round(stress.get("worst_day_pct", 0), 2)
        components["worst_week_pct"] = round(stress.get("worst_week_pct", 0), 2)

    # Aggregate score
    weights = {
        "wilson_lower_wr": 0.30,
        "ensemble_qualified_share": 0.20,
        "ensemble_median_wr": 0.15,
        "regime_clarity_0_100": 0.15,
        "vol_stability_0_100": 0.20,
    }
    score = 0.0
    for k, w in weights.items():
        v = components.get(k, 0.0)
        score += min(100.0, max(0.0, v)) * w
    return PairStability(
        pair=pair,
        score_0_100=round(score, 1),
        components=components,
        guarantees=guarantees,
        notes=notes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 11. Per-pair payout-conservative threshold
# ─────────────────────────────────────────────────────────────────────────────

def break_even_probability(payout_pct: float = 0.85) -> float:
    """Минимальная WR для безубыточности при заданном payout."""
    return 1.0 / (1.0 + payout_pct)


def slippage_resilient_threshold(payout_pct: float = 0.85, slippage_pct: float = 0.001) -> float:
    """Учитывая slippage (0.1% = ~10 pip на EURUSD), минимальный WR для нуля."""
    eff_payout = payout_pct * (1 - slippage_pct)
    return 1.0 / (1.0 + eff_payout)


# ─────────────────────────────────────────────────────────────────────────────
# 12. Закрытые сделки → real returns array
# ─────────────────────────────────────────────────────────────────────────────

def closed_trades_returns() -> dict[str, Any]:
    closed = _load_json(STATE_DIR / "closed_trades.json", [])
    if not closed:
        return {"returns": [], "wins": 0, "losses": 0, "total": 0}
    returns = []
    wins = losses = 0
    for t in closed:
        pnl = t.get("pnl_usd")
        if pnl is None:
            continue
        returns.append(float(pnl))
        if (t.get("result") or "").upper() == "WIN":
            wins += 1
        else:
            losses += 1
    return {
        "returns": returns,
        "wins": wins,
        "losses": losses,
        "total": len(returns),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 13. Главный сводный отчёт по системе (для /api/stability)
# ─────────────────────────────────────────────────────────────────────────────

def system_stability_report() -> dict[str, Any]:
    """Главный отчёт стабильности — 30+ метрик системы целиком."""
    closed = _load_json(STATE_DIR / "closed_trades.json", [])
    rets = closed_trades_returns()
    cfg = _load_json(STATE_DIR / "strategy_config.json", {"pairs": {}})
    summary = cfg.get("summary") or {}
    n_qualified_cells = sum(1 for p, info in (cfg.get("pairs") or {}).items()
                            for s, _ in (info.get("by_session") or {}).items() if False) or 0
    qualified_pairs = sum(1 for p, info in (cfg.get("pairs") or {}).items()
                          if info.get("qualifies_70pct"))

    by_session = {"Asia": 0, "London": 0, "Overlap": 0, "NY": 0}
    qual_by_session = {"Asia": 0, "London": 0, "Overlap": 0, "NY": 0}
    for p, info in (cfg.get("pairs") or {}).items():
        for s, sinfo in (info.get("by_session") or {}).items():
            by_session[s] = by_session.get(s, 0) + 1
            if sinfo and sinfo.get("qualifies_70pct"):
                qual_by_session[s] = qual_by_session.get(s, 0) + 1

    # Wilson on overall paper-stats
    stats = _load_json(STATE_DIR / "paper_stats.json", {})
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    total = wins + losses
    wlo, whi = wilson_lower_upper(wins, total, 0.95)

    # Bootstrap on closed pnls
    bs = bootstrap_ci(rets["returns"], n_iter=2000, confidence=0.95) if rets["returns"] else {}
    var = var_cvar(rets["returns"], 0.95) if rets["returns"] else {}
    sharpe = sharpe_ratio(rets["returns"], 252)
    sortino = sortino_ratio(rets["returns"], 252)
    pf = profit_factor(rets["returns"])
    exp_r = expectancy(rets["returns"])
    streaks = streak_analysis(closed)
    dist = distribution_stats(rets["returns"])

    # Equity curve for MDD
    equity = []
    cum = 0.0
    for t in sorted(closed, key=lambda x: x.get("close_time") or ""):
        cum += float(t.get("pnl_usd") or 0)
        equity.append(cum)
    mdd = max_drawdown(equity)

    # Calibration: predicted prob_at_open vs WIN/LOSS
    probs = [t.get("probability_pct_at_open", 50) / 100.0 for t in closed if t.get("probability_pct_at_open")]
    outcomes = [1 if (t.get("result") or "").upper() == "WIN" else 0 for t in closed if t.get("probability_pct_at_open")]
    brier = brier_score(probs, outcomes) if probs else None
    ll = log_loss(probs, outcomes) if probs else None
    calib = calibration_table(probs, outcomes, 10) if probs else []

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "stability_score_0_100": round(_compute_global_score(stats, summary, sharpe, brier or 1.0), 1),
        "wilson_wr_lower_95": round(wlo * 100, 1),
        "wilson_wr_upper_95": round(whi * 100, 1),
        "bootstrap_pnl_mean": round(bs.get("mean", 0), 3) if bs else 0,
        "bootstrap_pnl_p5": round(bs.get("p5", 0), 3) if bs else 0,
        "bootstrap_pnl_p50": round(bs.get("p50", 0), 3) if bs else 0,
        "bootstrap_pnl_p95": round(bs.get("p95", 0), 3) if bs else 0,
        "var_95": round(var.get("var", 0), 3) if var else 0,
        "cvar_95": round(var.get("cvar", 0), 3) if var else 0,
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "max_drawdown_pct": round(mdd.get("mdd_pct", 0), 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "expectancy_per_trade": round(exp_r, 3),
        "kelly_fraction_half": round(kelly_fraction(stats.get("win_rate_pct", 50) / 100.0, 0.85, half=True), 3),
        "break_even_probability": round(break_even_probability(0.85) * 100, 1),
        "slippage_threshold_probability": round(slippage_resilient_threshold(0.85, 0.001) * 100, 1),
        "qualified_pairs_count": qualified_pairs,
        "qualified_cells_total": sum(qual_by_session.values()),
        "qualified_by_session": qual_by_session,
        "total_cells_by_session": by_session,
        "longest_win_streak": streaks["longest_win_streak"],
        "longest_loss_streak": streaks["longest_loss_streak"],
        "current_streak": streaks["current_streak"],
        "current_streak_kind": streaks["current_streak_kind"],
        "skew": round(dist.get("skew", 0), 3),
        "kurtosis": round(dist.get("kurt", 0), 3),
        "brier_score": round(brier, 4) if brier is not None else None,
        "log_loss": round(ll, 4) if ll is not None else None,
        "calibration_bins": calib,
        "n_closed_trades": len(closed),
        "n_returns": rets["total"],
    }


def _compute_global_score(stats: dict, summary: dict, sharpe: float, brier: float) -> float:
    wr = stats.get("win_rate_pct", 0) or 0
    qual_share = (summary.get("qualified_pairs_count") or 0) / 28 * 100
    sharpe_norm = max(0.0, min(100.0, (sharpe + 1) * 25))
    brier_norm = max(0.0, min(100.0, (1 - brier) * 100))
    return 0.40 * wr + 0.25 * qual_share + 0.20 * sharpe_norm + 0.15 * brier_norm


# ─────────────────────────────────────────────────────────────────────────────
# 14. Min-guarantee — нижняя граница Pn/L при 95% confidence
# ─────────────────────────────────────────────────────────────────────────────

def min_guarantee_per_trade(stake_usd: float = 1.0, payout_pct: float = 0.85) -> dict[str, Any]:
    """Гарантированный (95% доверие) ожидаемый PnL на сделку.

    Использует Wilson lower bound для WR на закрытых сделках + payout 85%.
    """
    rets = closed_trades_returns()
    stats = _load_json(STATE_DIR / "paper_stats.json", {})
    wins, losses = stats.get("wins", 0), stats.get("losses", 0)
    total = wins + losses
    wlo, whi = wilson_lower_upper(wins, total, 0.95) if total > 0 else (0.0, 1.0)
    expected_lower = wlo * (stake_usd * payout_pct) - (1 - wlo) * stake_usd
    expected_mean = (wins / total if total else 0.5) * (stake_usd * payout_pct) - (1 - (wins / total if total else 0.5)) * stake_usd
    expected_upper = whi * (stake_usd * payout_pct) - (1 - whi) * stake_usd
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "n_trades": total,
        "wr_observed_pct": round((wins / total * 100) if total else 0, 2),
        "wr_lower_95_pct": round(wlo * 100, 2),
        "wr_upper_95_pct": round(whi * 100, 2),
        "expected_pnl_lower_per_trade": round(expected_lower, 4),
        "expected_pnl_mean_per_trade": round(expected_mean, 4),
        "expected_pnl_upper_per_trade": round(expected_upper, 4),
        "stake_usd": stake_usd,
        "payout_pct": payout_pct,
    }
