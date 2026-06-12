#!/usr/bin/env python3
"""Адаптивная портфельная система под правила JONY:
- минимум ~3 сделки/день, вход по рынку в момент слота, экспирация 5ч
- 8 пар, walk-forward, БЕЗ взгляда в будущее
Новые методы: 1) адаптивный отбор пар по скользящей форме (trailing winrate),
2) режим рынка по старшему ТФ, 3) ансамбль методов (голосование), 4) топ-K сетапов слота.
"""
import pandas as pd, numpy as np, requests
from collections import deque, defaultdict

PAIRS = ["EURUSD=X","GBPUSD=X","USDJPY=X","AUDUSD=X","USDCHF=X","USDCAD=X","NZDUSD=X","EURJPY=X"]
SLOTS = (0,5,10,15,20)

def fetch(symbol):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"interval":"60m","range":"730d"},
                     headers={"User-Agent":"Mozilla/5.0"}, timeout=60)
    res = r.json()["chart"]["result"][0]; q = res["indicators"]["quote"][0]
    return pd.DataFrame({"time": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                         "high": q["high"], "low": q["low"], "close": q["close"]}).dropna().reset_index(drop=True)

def rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100/(1 + up/dn.replace(0, np.nan))

def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def prep(sym):
    h = fetch(sym); c = h["close"]
    h["rsi"] = rsi(c)
    h["hi20"] = h["high"].rolling(20).max(); h["lo20"] = h["low"].rolling(20).min()
    h["pos"] = (c-h["lo20"])/(h["hi20"]-h["lo20"])
    tr = pd.concat([h["high"]-h["low"], (h["high"]-c.shift()).abs(), (h["low"]-c.shift()).abs()], axis=1).max(axis=1)
    h["atr"] = tr.rolling(14).mean()
    h["dist"] = (c-ema(c,50))/h["atr"]
    h["ret5"] = c.pct_change(5)
    h["e200sl"] = ema(c,200).diff(20)
    # H4-режим: наклон EMA50 на 4-часовом эквиваленте (без лукахеда — только прошлые бары)
    h["h4tr"] = ema(c, 200).diff(4)
    h["sym"] = sym.replace("=X",""); return h

def vote(row):
    """Ансамбль: каждый метод голосует BUY/SELL/0. Направление = большинство, score = |сумма|."""
    v = 0.0; reasons = []
    if row["pos"] <= 0.2: v += 1.2; reasons.append("низ диапазона")
    elif row["pos"] >= 0.85: v -= 0.8; reasons.append("верх диапазона")
    if row["rsi"] < 35: v += 0.8; reasons.append("RSI низкий")
    elif row["rsi"] > 68: v -= 0.8; reasons.append("RSI высокий")
    if row["ret5"] > 0.0015: v += 0.7; reasons.append("импульс вверх")   # A1 — за импульсом
    if row["dist"] < -1.8: v += 0.5
    elif row["dist"] > 1.8: v -= 0.3
    if row["h4tr"] > 0: v += 0.4   # старший ТФ вверх
    elif row["h4tr"] < 0: v -= 0.4
    d = "BUY" if v > 0 else "SELL"
    return d, abs(v), "+".join(reasons[:3]) if reasons else "ансамбль"

def main():
    data = {s: prep(s) for s in PAIRS}
    end = min(d["time"].max() for d in data.values()) - pd.Timedelta(hours=6)
    start = end - pd.Timedelta(days=365)
    slots = [t for t in pd.date_range(start.ceil("h"), end, freq="1h")
             if t.weekday() < 5 and t.hour in SLOTS]
    idx = {s: data[s].set_index("time") for s in data}

    # trailing форма пары: последние 40 виртуальных сделок этой пары (все сетапы, не только взятые)
    form = defaultdict(lambda: deque(maxlen=40))
    trades = []
    for t in slots:
        cands = []
        for s, h in idx.items():
            i = h.index.searchsorted(t, side="right") - 1
            if i < 220 or i+5 >= len(h): continue
            row = h.iloc[i]
            if row[["pos","rsi","dist","ret5"]].isna().any(): continue
            move = h["close"].iloc[i+5] - row["close"]
            if move == 0: continue
            d, sc, why = vote(row)
            if sc < 1.0: continue   # слабый консенсус — мимо
            f = form[row["sym"]]
            fwr = (sum(f)/len(f)) if len(f) >= 15 else 0.5
            cands.append({"t": t, "sym": row["sym"], "dir": d, "raw": sc,
                          "score": sc * (0.5 + fwr),  # форма пары взвешивает
                          "fwr": fwr, "why": why, "win": (move>0)==(d=="BUY")})
        # обновить форму ВСЕХ пар (виртуальные исходы), потом выбрать топ-1 слота
        for c_ in cands: form[c_["sym"]].append(c_["win"])
        if not cands: continue
        cands.sort(key=lambda x: -x["score"])
        best = cands[0]
        if best["fwr"] < 0.45: continue  # пара не в форме — пропуск слота
        trades.append(best)

    df = pd.DataFrame(trades); df["month"] = df["t"].astype(str).str[:7]
    print(f"АНСАМБЛЬ+ФОРМА: {100*df['win'].mean():.1f}% (n={len(df)}, ~{len(df)/261:.2f}/день)")
    mo = df.groupby("month")["win"].agg(["mean","size"])
    n65 = int((mo["mean"]>=0.65).sum())
    for m, r in mo.iterrows():
        print(f"  {m}: {100*r['mean']:.1f}% ({int(r['size'])}){'' if r['mean']>=0.65 else '  ←'}")
    print(f"мес≥65%: {n65}/{len(mo)}, худший {100*mo['mean'].min():.0f}%")
    print("\nПо парам:"); print(df.groupby("sym")["win"].agg(["mean","size"]).sort_values("mean", ascending=False))
    # вариант: только пары в хорошей форме (fwr>0.55)
    s = df[df["fwr"] > 0.55]
    if len(s):
        mo2 = s.groupby("month")["win"].agg(["mean","size"])
        print(f"\nТолько форма>55%: {100*s['win'].mean():.1f}% (n={len(s)}, ~{len(s)/261:.2f}/день) мес≥65%: {int((mo2['mean']>=0.65).sum())}/{len(mo2)}")
    df.to_csv("adaptive_trades.csv", index=False)

if __name__ == "__main__":
    main()
