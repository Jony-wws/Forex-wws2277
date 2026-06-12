#!/usr/bin/env python3
"""Исследование года: какие условия реально двигают winrate.
Использует backtest_trades_v2.csv (исход `up` = рынок вырос за 5ч)
+ свежие H1 для доп. фичей. Никакого взгляда в будущее: все фичи на момент входа.
"""
import pandas as pd, numpy as np, requests

def fetch(interval, rng, symbol="EURUSD=X"):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"interval": interval, "range": rng},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    res = r.json()["chart"]["result"][0]; q = res["indicators"]["quote"][0]
    return pd.DataFrame({"time": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                         "open": q["open"], "high": q["high"], "low": q["low"],
                         "close": q["close"]}).dropna().reset_index(drop=True)

def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def main():
    df = pd.read_csv("backtest_trades_v2.csv", parse_dates=["t"])
    df = df[df["move"] != 0].copy()
    h1 = fetch("60m", "730d").set_index("time")
    c = h1["close"]
    tr = pd.concat([(h1["high"]-h1["low"]),
                    (h1["high"]-c.shift()).abs(),
                    (h1["low"]-c.shift()).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()
    atr_pct = atr14.rolling(500).rank(pct=True)  # волатильность-режим
    e50 = ema(c, 50); dist_e50 = (c - e50) / atr14
    ret5 = c.pct_change(5)  # импульс последних 5ч
    # привязка фичей к моменту входа
    idx = h1.index
    feats = []
    for t in df["t"]:
        i = idx.searchsorted(t, side="right") - 1
        feats.append((float(atr_pct.iloc[i]) if i >= 0 else np.nan,
                      float(dist_e50.iloc[i]) if i >= 0 else np.nan,
                      float(ret5.iloc[i]) if i >= 0 else np.nan))
    df[["volp", "dist", "ret5"]] = pd.DataFrame(feats, index=df.index)
    df["dow"] = df["t"].dt.dayofweek
    df["win"] = df["win"].astype(bool); df["up"] = df["up"].astype(bool)

    def wr(mask, direction=None):
        s = df[mask] if mask is not None else df
        if direction is not None:
            w = (s["up"] == (direction == "BUY"))
            return 100*w.mean(), len(s)
        return 100*s["win"].mean(), len(s)

    base, n = wr(df.index == df.index)
    print(f"БАЗА V2: {base:.1f}% (n={n}), сделок/день ~{n/261:.1f}")

    print("\n=== 1. БЕЗ уменьшения сделок: где V2 ошибается → переворот/замена правила ===")
    cands = [
        ("слот 00 UTC", df["slot"] == 0), ("слот 05", df["slot"] == 5),
        ("слот 10", df["slot"] == 10), ("слот 15", df["slot"] == 15), ("слот 20", df["slot"] == 20),
        ("Пн", df["dow"] == 0), ("Вт", df["dow"] == 1), ("Ср", df["dow"] == 2),
        ("Чт", df["dow"] == 3), ("Пт", df["dow"] == 4),
        ("тихий рынок volp<0.3", df["volp"] < 0.3),
        ("средний 0.3-0.7", (df["volp"] >= 0.3) & (df["volp"] < 0.7)),
        ("бурный volp>=0.7", df["volp"] >= 0.7),
        ("растянуто вверх dist>1.5 ATR", df["dist"] > 1.5),
        ("растянуто вниз dist<-1.5", df["dist"] < -1.5),
        ("около EMA |dist|<0.5", df["dist"].abs() < 0.5),
        ("импульс 5ч вверх >0.15%", df["ret5"] > 0.0015),
        ("импульс 5ч вниз <-0.15%", df["ret5"] < -0.0015),
        ("pos<=0.15 (низ)", df["pos"] <= 0.15), ("pos>=0.85 (верх)", df["pos"] >= 0.85),
        ("середина 0.35-0.65", (df["pos"] > 0.35) & (df["pos"] < 0.65)),
        ("RSI 40-60", (df["rsi"] >= 40) & (df["rsi"] <= 60)),
        ("был свип", df["sweep"].notna() & (df["sweep"] != "")),
    ]
    rows = []
    for name, m in cands:
        v2, k = wr(m)
        buy, _ = wr(m, "BUY"); sell, _ = wr(m, "SELL")
        rows.append((name, k, v2, buy, sell))
    for name, k, v2, buy, sell in rows:
        flag = " ←" if max(buy, sell) - v2 > 4 and k > 60 else ""
        print(f"{name:32s} n={k:4d}  V2={v2:5.1f}%  всё-BUY={buy:5.1f}%  всё-SELL={sell:5.1f}%{flag}")

    print("\n=== 2. Селективность: фильтры, поднимающие % (меньше сделок) ===")
    sel = [
        ("только pos<=0.2 → BUY", (df["pos"] <= 0.2), "BUY"),
        ("только pos<=0.3 → BUY", (df["pos"] <= 0.3), "BUY"),
        ("pos<=0.2 BUY + тихий рынок", (df["pos"] <= 0.2) & (df["volp"] < 0.5), "BUY"),
        ("pos<=0.2 BUY + RSI<40", (df["pos"] <= 0.2) & (df["rsi"] < 40), "BUY"),
        ("pos<=0.2 BUY + dist<-1", (df["pos"] <= 0.2) & (df["dist"] < -1), "BUY"),
        ("pos>=0.85 SELL + RSI>65", (df["pos"] >= 0.85) & (df["rsi"] > 65), "SELL"),
        ("RSI<=25 BUY", df["rsi"] <= 25, "BUY"),
        ("RSI<=30 BUY", df["rsi"] <= 30, "BUY"),
        ("RSI>=70 SELL", df["rsi"] >= 70, "SELL"),
        ("V2 только A+ (basis+sm_strong)", df["why"].str.contains("низа|RSI", na=False), None),
        ("V2 в слотах 10+15", df["slot"].isin([10, 15]), None),
        ("V2 тихий рынок", df["volp"] < 0.4, None),
        ("V2 Вт-Чт", df["dow"].isin([1, 2, 3]), None),
    ]
    for name, m, d in sel:
        w, k = wr(m, d)
        per_day = k / 261
        # помесячная стабильность
        s = df[m]
        if d is not None:
            ww = (s["up"] == (d == "BUY"))
        else:
            ww = s["win"]
        mo = s.assign(w=ww).groupby("month")["w"].agg(["mean", "size"])
        worst = 100*mo["mean"].min() if len(mo) else 0
        n70 = int((mo["mean"] >= 0.7).sum())
        print(f"{name:34s} n={k:4d} (~{per_day:.2f}/день)  WR={w:5.1f}%  худш.мес={worst:4.1f}%  мес≥70%: {n70}/{len(mo)}")

    print("\n=== 3. Комбинации направления (все сделки, без фильтра) ===")
    # кандидат: контртренд при растяжении + тренд на откате + низ/верх диапазона
    def rule(r):
        if r["pos"] <= 0.15: return "BUY"
        if r["pos"] >= 0.92 and r["rsi"] >= 65: return "SELL"
        if r["rsi"] <= 25: return "BUY"
        if r["rsi"] >= 75: return "SELL"
        if r["dist"] > 1.8: return "SELL"        # сильно растянуто вверх → откат
        if r["dist"] < -1.8: return "BUY"
        if abs(r["sm_score"]) >= 3.5 and isinstance(r["sm_bias"], str): return r["sm_bias"]
        if r["score"] > 0.3:  return "BUY" if r["pos"] <= 0.5 else "SELL"
        if r["score"] < -0.3: return "SELL" if r["pos"] >= 0.5 else "BUY"
        if isinstance(r["sm_bias"], str) and r["sm_bias"]: return r["sm_bias"]
        return "BUY" if r["ret5"] < 0 else "SELL"  # возврат к среднему
    pred = df.apply(rule, axis=1)
    w3 = (df["up"] == (pred == "BUY"))
    mo = df.assign(w=w3).groupby("month")["w"].agg(["mean", "size"])
    print(f"V3-кандидат: {100*w3.mean():.1f}% (n={len(df)})")
    for m_, r_ in mo.iterrows():
        print(f"  {m_}: {100*r_['mean']:.1f}% (n={int(r_['size'])})")

if __name__ == "__main__":
    main()
