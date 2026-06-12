#!/usr/bin/env python3
"""Годовой бэктест системы JONY (5ч экспирация, слоты 00/05/10/15/20 UTC).

Честный walk-forward: на каждый слот берём ТОЛЬКО данные до момента входа
(D1/H4 пересобираются из H1-среза — никакого взгляда в будущее).
Варианты:
  A  как живой сканер: крупные игроки (основа) -> тренд -> наклон EMA
  D  A + если свежий свип ПРОТИВ направления -> идём ЗА свипом (урок 11.06)
  E  A + во флэте у краёв 20-барного диапазона -> возврат к среднему
  F  D + E вместе
Выход: winrate общий / по месяцам / по качеству / по слотам.
"""
import os, sys, json
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from scanner import trend_state, rsi, ema, atr, resample_4h, liquidity_traps
from smart_money import analyze_smart_money

SLOTS = (0, 5, 10, 15, 20)
PIP = 1e-4


def fetch(interval, rng, symbol="EURUSD=X"):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"interval": interval, "range": rng},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"time": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                       "open": q["open"], "high": q["high"],
                       "low": q["low"], "close": q["close"]}).dropna()
    return df.reset_index(drop=True)


def resample_d1(h1):
    df = h1.set_index("time").resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return df.reset_index()


def base_direction(h1s, h4s, d1s, sm):
    """Реплика иерархии живого сканера (уровень 1 крупные -> уровень 3 тренд)."""
    d1_tr, h4_tr, h1_tr = trend_state(d1s), trend_state(h4s), trend_state(h1s)
    sc = lambda t: 1 if t == "up" else (-1 if t == "down" else 0)
    score = 2.0 * sc(d1_tr) + 1.5 * sc(h4_tr) + 1.0 * sc(h1_tr)
    if sm and sm.get("strong") and sm.get("bias"):
        return sm["bias"], score, True
    if score > 0.3:
        return "BUY", score, False
    if score < -0.3:
        return "SELL", score, False
    if sm and sm.get("bias"):
        return sm["bias"], score, False
    slope = float(ema(h1s["close"], 50).diff().iloc[-1])
    return ("BUY" if slope >= 0 else "SELL"), score, False


def run():
    h1 = fetch("60m", "730d")
    print("h1 bars:", len(h1), h1["time"].min(), "->", h1["time"].max())
    h1c = h1.set_index("time")["close"]

    end = h1["time"].max() - timedelta(hours=6)
    start = end - timedelta(days=365)
    rows = []
    t0 = start.replace(minute=0, second=0, microsecond=0)
    cur = t0
    n = 0
    while cur <= end:
        if cur.weekday() < 5 and cur.hour in SLOTS:
            sl = h1[h1["time"] <= cur]
            if len(sl) >= 250:
                # entry/exit prices
                entry_t = sl["time"].iloc[-1]
                fut = h1[(h1["time"] > cur) & (h1["time"] <= cur + timedelta(hours=5, minutes=30))]
                if not fut.empty and (fut["time"].iloc[-1] - cur) >= timedelta(hours=4):
                    entry = float(sl["close"].iloc[-1])
                    exit_ = float(fut["close"].iloc[-1])
                    h4s = resample_4h(sl)
                    d1s = resample_d1(sl)
                    try:
                        traps = liquidity_traps(sl, d1s, cur)
                    except Exception:
                        traps = None
                    try:
                        sm = analyze_smart_money(sl, sl, h4s, d1s, traps, cur)
                    except Exception:
                        sm = None
                    d, score, basis = base_direction(sl, h4s, d1s, sm)

                    h1_rsi = float(rsi(sl["close"]).iloc[-1])
                    lo20 = float(sl["low"].tail(20).min()); hi20 = float(sl["high"].tail(20).max())
                    pos = (entry - lo20) / max(hi20 - lo20, 1e-6)
                    sweep_bias = (traps["sweeps"][0]["bias"] if traps and traps.get("sweeps") else None)
                    rows.append(dict(t=cur, entry=entry, exit=exit_, dir=d, score=score,
                                     basis=basis, sm_bias=(sm or {}).get("bias"),
                                     sm_score=(sm or {}).get("score", 0),
                                     rsi=h1_rsi, pos=pos, sweep=sweep_bias,
                                     month=cur.strftime("%Y-%m"), slot=cur.hour))
                    n += 1
                    if n % 100 == 0:
                        print("…", n, cur.date())
        cur += timedelta(hours=1)

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(HERE, "backtest_trades.csv"), index=False)
    print("trades:", len(df))
    evaluate(df)


def variant_dir(r, variant):
    d = r["dir"]
    if variant in ("D", "F") and r["sweep"] and r["sweep"] != d:
        d = r["sweep"]
    if variant in ("E", "F") and not r["basis"] and abs(r["score"]) < 0.8:
        if r["pos"] >= 0.8 or r["rsi"] >= 70:
            d = "SELL"
        elif r["pos"] <= 0.2 or r["rsi"] <= 30:
            d = "BUY"
    return d


def evaluate(df):
    for variant in ["A", "D", "E", "F"]:
        dirs = df.apply(lambda r: variant_dir(r, variant), axis=1)
        move = df["exit"] - df["entry"]
        win = np.where(dirs == "BUY", move > 0, move < 0)
        draw = move == 0
        ok = win[~draw]
        print(f"\n=== Вариант {variant}: {ok.mean()*100:.1f}% winrate "
              f"({ok.sum()}/{len(ok)}, draws {draw.sum()})")
        d2 = df.assign(win=win, draw=draw)
        d2 = d2[~d2["draw"]]
        mon = d2.groupby("month")["win"].agg(["mean", "count"])
        for m, r in mon.iterrows():
            bar = "#" * int(r["mean"] * 30)
            print(f"  {m}: {r['mean']*100:5.1f}%  n={int(r['count']):3d} {bar}")
        print("  по слотам (UTC):")
        for s, r in d2.groupby("slot")["win"].agg(["mean", "count"]).iterrows():
            print(f"    {s:02d}: {r['mean']*100:5.1f}% n={int(r['count'])}")
        print("  основа крупных:", end=" ")
        for b, r in d2.groupby("basis")["win"].agg(["mean", "count"]).iterrows():
            print(f"basis={b}: {r['mean']*100:.1f}% (n={int(r['count'])})", end="  ")
        print()


if __name__ == "__main__":
    run()
