"""Тест экспираций: те же сигналы (ансамбль, 3 пары и 8 пар), исходы при 1/2/3/4/5/6/8ч."""
import pandas as pd, numpy as np
import adaptive_study as A
from collections import deque, defaultdict

HORIZONS = [1,2,3,4,5,6,8]

def run(pairs):
    data = {s: A.prep(s) for s in pairs}
    end = min(d["time"].max() for d in data.values()) - pd.Timedelta(hours=10)
    start = end - pd.Timedelta(days=365)
    slots = [t for t in pd.date_range(start.ceil("h"), end, freq="1h")
             if t.weekday() < 5 and t.hour in A.SLOTS]
    idx = {s: data[s].set_index("time") for s in data}
    form = defaultdict(lambda: deque(maxlen=40))
    trades = []
    for t in slots:
        cands = []
        for s, h in idx.items():
            i = h.index.searchsorted(t, side="right") - 1
            if i < 220 or i+max(HORIZONS) >= len(h): continue
            row = h.iloc[i]
            if row[["pos","rsi","dist","ret5"]].isna().any(): continue
            d, sc, why = A.vote(row)
            if sc < 1.0: continue
            wins = {}
            ok = True
            for hz in HORIZONS:
                mv = h["close"].iloc[i+hz] - row["close"]
                if mv == 0: ok = False; break
                wins[hz] = (mv > 0) == (d == "BUY")
            if not ok: continue
            f = form[row["sym"]]
            fwr = (sum(f)/len(f)) if len(f) >= 15 else 0.5
            cands.append({"t": t, "sym": row["sym"], "score": sc*(0.5+fwr), "fwr": fwr, **{f"w{hz}": wins[hz] for hz in HORIZONS}})
        for c_ in cands: form[c_["sym"]].append(c_["w5"])
        if not cands: continue
        cands.sort(key=lambda x: -x["score"])
        b = cands[0]
        if b["fwr"] < 0.45: continue
        trades.append(b)
    df = pd.DataFrame(trades)
    df["month"] = df["t"].astype(str).str[:7]
    return df

for name, pairs in [("3 пары", ["EURUSD=X","EURJPY=X","USDCHF=X"]), ("8 пар", A.PAIRS)]:
    df = run(pairs)
    print(f"\n=== {name} (n={len(df)}, ~{len(df)/261:.2f}/день) ===")
    for hz in HORIZONS:
        col = f"w{hz}"
        mo = df.groupby("month")[col].mean()
        print(f"  экспирация {hz}ч: {100*df[col].mean():.1f}%  мес≥65%: {int((mo>=0.65).sum())}/{len(mo)}  худш {100*mo.min():.0f}%")
    # помесячно для лучшей экспирации
    best = max(HORIZONS, key=lambda hz: df[f"w{hz}"].mean())
    print(f"  ЛУЧШАЯ: {best}ч, помесячно:")
    mo = df.groupby("month")[f"w{best}"].agg(["mean","size"])
    for m, r in mo.iterrows(): print(f"    {m}: {100*r['mean']:.1f}% ({int(r['size'])})")
