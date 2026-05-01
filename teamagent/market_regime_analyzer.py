"""market_regime_analyzer — анализ поведения рынка за 365 дней по каждой паре.

Что делает:
  1. Качает 2 года 1H истории Yahoo по каждой из 28 пар (period="2y").
  2. Из них берёт **последние 365 дней** ("год реальной истории") — это и есть
     «как себя вёл рынок за год» (запрос пользователя 2026-05-01).
  3. Для каждого 1H бара считает:
        - dow (0=Mon..6=Sun)
        - hour (0..23 UTC)
        - session ("Asia"/"London"/"Overlap"/"NY"/"Off")
        - abs_return = |close_t / close_(t-1) - 1|  (в долях)
        - direction = sign(close_t - close_(t-1))
        - is_high_vol = 1 если abs_return ≥ p90 для этой пары за 365 дней
  4. Строит сводки:
        a) per-pair × per-session × per-dow:  count, mean_abs_ret, p90_abs_ret,
           up_share (доля баров с положительной свечой).
        b) per-pair × per-hour:  то же.
        c) hot_hours_utc:  топ-5 часов UTC с самой высокой mean_abs_ret —
           это эмпирически совпадает с NFP / FOMC / ECB / BoE / CPI окнами.
        d) high_vol_clusters: для каждой пары — список (dow, hour) где доля
           is_high_vol баров ≥ 25% (минимум 10 наблюдений). Это «когда
           реально движется именно эта пара» — сезонный профиль реакций.
  5. Сохраняет в state/market_regime_365d.json.

Использование:
    python -m teamagent.market_regime_analyzer            # один прогон, ~3-5 мин
    python -m teamagent.market_regime_analyzer --pair EURUSD   # одна пара

ВАЖНО:
  - НИКАКИХ симуляторов, всё на реальной истории Yahoo.
  - Этот модуль **не блокирует** трейдинг и **не меняет** гейт 70%; это
    диагностика «как реагирует рынок на типовые события» через статистику
    реальных движений в типовые часы (NFP пятница 12:30 UTC = эмпирически
    стабильно попадает в hot_hours без нужды парсить календарь новостей).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .data import yahoo
from .strategies import SESSION_WINDOWS

log = logging.getLogger("market_regime_analyzer")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "market_regime_analyzer.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

OUTPUT_FILE = config.STATE_DIR / "market_regime_365d.json"
LOOKBACK_DAYS = 365


def _session_for(hour: int) -> str:
    for name, (s, e) in SESSION_WINDOWS.items():
        if s <= hour < e:
            return name
    return "Off"


def analyze_pair(pair: str) -> dict | None:
    """Анализ одной пары на 365 днях. Возвращает dict со статистикой."""
    bars = yahoo.fetch(pair, interval="1h", period="2y")
    if bars is None or bars.empty or len(bars) < 200:
        log.warning(f"{pair}: недостаточно данных (bars={0 if bars is None else len(bars)})")
        return None

    # отрезаем последние 365 дней
    cutoff = bars.index[-1] - timedelta(days=LOOKBACK_DAYS)
    bars = bars.loc[bars.index >= cutoff].copy()
    if len(bars) < 200:
        log.warning(f"{pair}: после среза 365д осталось {len(bars)} баров")
        return None

    close = bars["Close"].astype(float)
    ret = close.pct_change().fillna(0.0)
    abs_ret = ret.abs()
    p90 = float(abs_ret.quantile(0.90))
    mean_abs = float(abs_ret.mean())
    p99 = float(abs_ret.quantile(0.99))

    # обогащаем dataframe
    df = pd.DataFrame({
        "close": close,
        "ret": ret,
        "abs_ret": abs_ret,
        "is_high_vol": (abs_ret >= p90).astype(int),
        "is_up": (ret > 0).astype(int),
    })
    df["dow"] = df.index.dayofweek         # 0=Mon..6=Sun
    df["hour"] = df.index.hour
    df["session"] = df["hour"].map(_session_for)

    # a) per session × dow
    by_session_dow = []
    for session in list(SESSION_WINDOWS.keys()) + ["Off"]:
        for dow in range(7):
            sub = df[(df["session"] == session) & (df["dow"] == dow)]
            n = len(sub)
            if n < 5:
                continue
            by_session_dow.append({
                "session": session,
                "dow": int(dow),
                "n_bars": int(n),
                "mean_abs_ret_bp": round(float(sub["abs_ret"].mean()) * 1e4, 2),
                "p90_abs_ret_bp": round(float(sub["abs_ret"].quantile(0.90)) * 1e4, 2),
                "up_share_pct": round(float(sub["is_up"].mean()) * 100.0, 1),
                "high_vol_share_pct": round(float(sub["is_high_vol"].mean()) * 100.0, 1),
            })

    # b) per hour (UTC)
    by_hour = []
    for h in range(24):
        sub = df[df["hour"] == h]
        if len(sub) < 5:
            continue
        by_hour.append({
            "hour_utc": int(h),
            "session": _session_for(h),
            "n_bars": int(len(sub)),
            "mean_abs_ret_bp": round(float(sub["abs_ret"].mean()) * 1e4, 2),
            "high_vol_share_pct": round(float(sub["is_high_vol"].mean()) * 100.0, 1),
            "up_share_pct": round(float(sub["is_up"].mean()) * 100.0, 1),
        })
    # топ-5 hot часов
    hot_hours = sorted(by_hour, key=lambda x: -x["mean_abs_ret_bp"])[:5]
    hot_hours_summary = [{"hour_utc": h["hour_utc"],
                          "mean_abs_ret_bp": h["mean_abs_ret_bp"],
                          "session": h["session"]} for h in hot_hours]

    # d) high_vol_clusters: (dow, hour) с долей high_vol ≥ 25%
    clusters = []
    grp = df.groupby(["dow", "hour"])
    for (dow, hour), sub in grp:
        n = len(sub)
        if n < 10:
            continue
        share = float(sub["is_high_vol"].mean())
        if share >= 0.25:
            clusters.append({
                "dow": int(dow),
                "hour_utc": int(hour),
                "session": _session_for(int(hour)),
                "n": int(n),
                "high_vol_share_pct": round(share * 100.0, 1),
                "mean_abs_ret_bp": round(float(sub["abs_ret"].mean()) * 1e4, 2),
            })
    clusters.sort(key=lambda x: -x["high_vol_share_pct"])

    return {
        "pair": pair,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "n_bars": int(len(df)),
        "first_bar_utc": df.index[0].isoformat(),
        "last_bar_utc": df.index[-1].isoformat(),
        "vol_thresholds": {
            "mean_abs_ret_bp": round(mean_abs * 1e4, 2),
            "p90_abs_ret_bp": round(p90 * 1e4, 2),
            "p99_abs_ret_bp": round(p99 * 1e4, 2),
        },
        "by_session_dow": by_session_dow,
        "by_hour": by_hour,
        "hot_hours_utc": hot_hours_summary,
        "high_vol_clusters": clusters[:25],   # топ-25
    }


def analyze_all(pairs: list[str] | None = None) -> dict:
    pairs = pairs or list(config.PAIRS)
    started = datetime.now(timezone.utc)
    results: dict[str, dict] = {}
    for i, p in enumerate(pairs, 1):
        log.info(f"[{i}/{len(pairs)}] analyzing {p}...")
        out = analyze_pair(p)
        if out is None:
            results[p] = {"pair": p, "note": "insufficient history"}
        else:
            results[p] = out
            log.info(
                f"  → {p}: bars={out['n_bars']}, "
                f"hot_hours={[h['hour_utc'] for h in out['hot_hours_utc']]}, "
                f"clusters={len(out['high_vol_clusters'])}"
            )

    finished = datetime.now(timezone.utc)

    # глобальный взгляд: hot часы поверх всех пар (среднее mean_abs_ret_bp)
    hour_to_avg: dict[int, list[float]] = {}
    for p, data in results.items():
        if "by_hour" not in data:
            continue
        for row in data["by_hour"]:
            hour_to_avg.setdefault(row["hour_utc"], []).append(row["mean_abs_ret_bp"])
    global_hot = [
        {"hour_utc": h, "session": _session_for(h),
         "mean_abs_ret_bp_avg": round(float(np.mean(v)), 2),
         "n_pairs": len(v)}
        for h, v in hour_to_avg.items()
    ]
    global_hot.sort(key=lambda x: -x["mean_abs_ret_bp_avg"])

    return {
        "as_of": finished.isoformat(),
        "duration_sec": int((finished - started).total_seconds()),
        "lookback_days": LOOKBACK_DAYS,
        "pairs_analyzed": len(results),
        "global_hot_hours_utc_top10": global_hot[:10],
        "pairs": results,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default=None, help="analyze only this pair")
    args = ap.parse_args()

    if args.pair:
        out = analyze_pair(args.pair)
        if out is None:
            print(f"{args.pair}: no data", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    log.info("market_regime_analyzer start (28 pairs × 365d)")
    t0 = time.time()
    out = analyze_all()
    OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log.info(f"done in {time.time()-t0:.1f}s — wrote {OUTPUT_FILE} "
             f"({out['pairs_analyzed']} pairs)")


if __name__ == "__main__":
    main()
