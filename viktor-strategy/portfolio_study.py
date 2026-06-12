#!/usr/bin/env python3
"""Портфельная система: каждые 5ч сканируем 8 основных пар,
ранжируем сетапы по качеству, берём ТОЛЬКО лучшие. Walk-forward 365д, без взгляда в будущее.
Цель JONY: минимум 65% каждый месяц."""
import pandas as pd, numpy as np, requests, itertools, json

PAIRS = ["EURUSD=X","GBPUSD=X","USDJPY=X","AUDUSD=X","USDCHF=X","USDCAD=X","NZDUSD=X","EURJPY=X"]
SLOTS = (0,5,10,15,20)

def fetch(symbol):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"interval":"60m","range":"730d"},
                     headers={"User-Agent":"Mozilla/5.0"}, timeout=60)
    res = r.json()["chart"]["result"][0]; q = res["indicators"]["quote"][0]
    return pd.DataFrame({"time": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                         "open": q["open"], "high": q["high"], "low": q["low"],
                         "close": q["close"]}).dropna().reset_index(drop=True)

def rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100/(1 + up/dn.replace(0, np.nan))

def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def prep(sym):
    h = fetch(sym)
    c = h["close"]
    h["rsi"] = rsi(c)
    h["hi20"] = h["high"].rolling(20).max(); h["lo20"] = h["low"].rolling(20).min()
    h["pos"] = (c - h["lo20"]) / (h["hi20"] - h["lo20"])
    tr = pd.concat([h["high"]-h["low"], (h["high"]-c.shift()).abs(), (h["low"]-c.shift()).abs()], axis=1).max(axis=1)
    h["atr"] = tr.rolling(14).mean()
    h["dist"] = (c - ema(c,50)) / h["atr"]
    h["ret5"] = c.pct_change(5)
    h["e200_sl"] = ema(c,200).diff(20)          # долгий уклон
    h["rng"] = (h["hi20"] - h["lo20"]) / h["atr"]  # ширина диапазона в ATR
    h["sym"] = sym.replace("=X","")
    return h

def candidates(row):
    """Кандидаты сетапов в момент времени. Возвращает (dir, score) или None."""
    out = []
    if pd.isna(row["pos"]) or pd.isna(row["rsi"]) or pd.isna(row["dist"]): return out
    # 1. Низ диапазона → BUY (главный эдж)
    if row["pos"] <= 0.2 and row["rsi"] < 45:
        score = (0.2 - row["pos"])*8 + (45 - row["rsi"])/12
        if row["e200_sl"] > 0: score += 0.5         # долгий тренд вверх = отскоки сильнее
        if row["rng"] >= 2.5: score += 0.3          # широкий диапазон = есть куда отскочить
        out.append(("BUY", score, "низ диапазона"))
    # 2. Верх диапазона → SELL (слабее, требуем больше подтверждений)
    if row["pos"] >= 0.85 and row["rsi"] > 62 and row["e200_sl"] < 0:
        score = (row["pos"] - 0.85)*8 + (row["rsi"] - 62)/12
        out.append(("SELL", score, "верх диапазона + тренд вниз"))
    # 3. RSI-экстремум
    if row["rsi"] <= 28:
        out.append(("BUY", 1.0 + (28 - row["rsi"])/8, "RSI перепроданность"))
    if row["rsi"] >= 75 and row["e200_sl"] < 0:
        out.append(("SELL", 1.0 + (row["rsi"] - 75)/8, "RSI перекупленность"))
    return out

def main():
    data = {}
    for s in PAIRS:
        try: data[s] = prep(s)
        except Exception as e: print("FAIL", s, e)
    end = min(d["time"].max() for d in data.values()) - pd.Timedelta(hours=6)
    start = end - pd.Timedelta(days=365)

    # все слоты
    slots = pd.date_range(start.ceil("h"), end, freq="1h")
    slots = [t for t in slots if t.weekday() < 5 and t.hour in SLOTS]

    idx = {s: data[s].set_index("time") for s in data}
    trades = []
    for t in slots:
        cands = []
        for s, h in idx.items():
            i = h.index.searchsorted(t, side="right") - 1
            if i < 220: continue
            row = h.iloc[i]
            j = i + 5
            if j >= len(h): continue
            move = h["close"].iloc[j] - row["close"]
            if move == 0: continue
            for d, sc, why in candidates(row):
                cands.append({"t": t, "sym": row["sym"], "dir": d, "score": sc, "why": why,
                              "win": (move > 0) == (d == "BUY")})
        if not cands: continue
        cands.sort(key=lambda x: -x["score"])
        trades.append(cands[0])  # ТОЛЬКО лучший сетап слота

    df = pd.DataFrame(trades)
    df["month"] = df["t"].astype(str).str[:7]
    print(f"ИТОГО: {100*df['win'].mean():.1f}% (n={len(df)}, ~{len(df)/261:.2f}/день)")
    mo = df.groupby("month")["win"].agg(["mean","size"])
    bad = 0
    for m, r in mo.iterrows():
        mark = "" if r["mean"] >= 0.65 else "  ← ниже 65"
        if r["mean"] < 0.65: bad += 1
        print(f"  {m}: {100*r['mean']:.1f}% (n={int(r['size'])}){mark}")
    print(f"месяцев ≥65%: {len(mo)-bad}/{len(mo)}")
    print("\nПо сетапам:"); print(df.groupby("why")["win"].agg(["mean","size"]))
    print("\nПо парам:"); print(df.groupby("sym")["win"].agg(["mean","size"]).sort_values("mean", ascending=False))
    # с порогом score
    for thr in (0.5, 0.8, 1.0, 1.2):
        s = df[df["score"] >= thr]
        if len(s) < 100: continue
        mo = s.groupby("month")["win"].agg(["mean","size"])
        n65 = int((mo["mean"] >= 0.65).sum())
        print(f"score≥{thr}: {100*s['win'].mean():.1f}% (n={len(s)}, ~{len(s)/261:.2f}/день) мес≥65%: {n65}/{len(mo)} худш={100*mo['mean'].min():.0f}%")
    df.to_csv("portfolio_trades.csv", index=False)

if __name__ == "__main__":
    main()
