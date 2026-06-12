"""Срезы по часам слота / дню недели / волатильности для 3-парной системы, экспирация 3ч."""
import pandas as pd, numpy as np
import adaptive_study as A
from collections import deque, defaultdict

PAIRS = ["EURUSD=X","EURJPY=X","USDCHF=X"]
data = {s: A.prep(s) for s in PAIRS}
end = min(d["time"].max() for d in data.values()) - pd.Timedelta(hours=10)
start = end - pd.Timedelta(days=365)
slots = [t for t in pd.date_range(start.ceil("h"), end, freq="1h")
         if t.weekday() < 5 and t.hour in A.SLOTS]
idx = {s: data[s].set_index("time") for s in data}
form = defaultdict(lambda: deque(maxlen=40))
rows = []
for t in slots:
    cands = []
    for s, h in idx.items():
        i = h.index.searchsorted(t, side="right") - 1
        if i < 220 or i+3 >= len(h): continue
        r = h.iloc[i]
        if r[["pos","rsi","dist","ret5"]].isna().any(): continue
        d, sc, why = A.vote(r)
        if sc < 1.0: continue
        mv = h["close"].iloc[i+3] - r["close"]
        if mv == 0: continue
        f = form[r["sym"]]
        fwr = (sum(f)/len(f)) if len(f) >= 15 else 0.5
        atr_now = r["atr"]; atr_med = h["atr"].iloc[i-100:i].median()
        cands.append({"t": t, "hour": t.hour, "dow": t.weekday(), "sym": r["sym"],
                      "score": sc*(0.5+fwr), "fwr": fwr, "volr": atr_now/atr_med,
                      "win": (mv>0)==(d=="BUY")})
    for c_ in cands: form[c_["sym"]].append(c_["win"])
    if not cands: continue
    cands.sort(key=lambda x: -x["score"])
    b = cands[0]
    if b["fwr"] < 0.45: continue
    rows.append(b)

df = pd.DataFrame(rows); df["month"] = df["t"].astype(str).str[:7]
print(f"БАЗА 3ч: {100*df['win'].mean():.1f}% n={len(df)} ~{len(df)/261:.2f}/день")
print("\nПо часу слота (UTC):"); print(df.groupby("hour")["win"].agg(["mean","size"]))
print("\nПо дню недели (0=пн):"); print(df.groupby("dow")["win"].agg(["mean","size"]))
print("\nПо волатильности:"); print(df.groupby(pd.cut(df["volr"], [0,0.8,1.1,1.4,10]))["win"].agg(["mean","size"]))

def rep(s, name):
    mo = s.groupby("month")["win"].agg(["mean","size"])
    print(f"\n{name}: {100*s['win'].mean():.1f}% n={len(s)} ~{len(s)/261:.2f}/день мес≥65%: {int((mo['mean']>=0.65).sum())}/{len(mo)} худш {100*mo['mean'].min():.0f}%")
    for m, r in mo.iterrows(): print(f"  {m}: {100*r['mean']:.1f}% ({int(r['size'])})")

best_hours = df.groupby("hour")["win"].mean().nlargest(3).index.tolist()
print("\nЛучшие 3 часа:", best_hours)
rep(df[df["hour"].isin(best_hours)], f"Только слоты {best_hours}")
bad_dow = df.groupby("dow")["win"].mean().idxmin()
rep(df[(df["hour"].isin(best_hours)) & (df["dow"]!=bad_dow)], f"слоты {best_hours} без дня {bad_dow}")
rep(df[(df["hour"].isin(best_hours)) & (df["volr"]<1.4)], f"слоты {best_hours} + vol<1.4")
df.to_csv("slots_trades.csv", index=False)
