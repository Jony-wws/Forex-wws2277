"""ML walk-forward: HistGradientBoosting на всех факторах, обучение только на прошлом,
тест на следующем месяце. 8 пар, вход по рынку, экспирация 3ч, слоты 0/5/10/15/20 UTC."""
import pandas as pd, numpy as np
import adaptive_study as A
from sklearn.ensemble import HistGradientBoostingClassifier

PAIRS = A.PAIRS
USD_LONG = {"EURUSD","GBPUSD","AUDUSD","NZDUSD"}  # рост = слабый доллар

def build_dataset():
    data = {}
    for s in PAIRS:
        h = A.prep(s)
        c = h["close"]
        lo24 = h["low"].rolling(24).min().shift(1); hi24 = h["high"].rolling(24).max().shift(1)
        h["sw_lo"] = ((h["low"] < lo24) & (c > lo24)).rolling(3).max()
        h["sw_hi"] = ((h["high"] > hi24) & (c < hi24)).rolling(3).max()
        h["ret1"] = c.pct_change(1); h["ret24"] = c.pct_change(24)
        h["atrr"] = h["atr"] / h["atr"].rolling(100).median()
        h["rng_w"] = (h["hi20"]-h["lo20"])/h["atr"]
        data[s] = h
    end = min(d["time"].max() for d in data.values()) - pd.Timedelta(hours=6)
    start = end - pd.Timedelta(days=720)
    slots = [t for t in pd.date_range(start.ceil("h"), end, freq="1h")
             if t.weekday() < 5 and t.hour in A.SLOTS]
    idx = {s: data[s].set_index("time") for s in data}
    rows = []
    for t in slots:
        # долларовый фактор: средний ret5 в "сторону слабого доллара"
        usd = []
        snap = {}
        for s, h in idx.items():
            i = h.index.searchsorted(t, side="right") - 1
            if i < 220 or i+3 >= len(h): continue
            r = h.iloc[i]
            if r[["pos","rsi","dist","ret5","atrr"]].isna().any(): continue
            snap[s] = (i, r)
            sym = r["sym"]
            usd.append(r["ret5"] if sym in USD_LONG else -r["ret5"])
        if not snap: continue
        usdf = float(np.mean(usd))
        for s, (i, r) in snap.items():
            h = idx[s]
            mv = h["close"].iloc[i+3] - r["close"]
            if mv == 0: continue
            rows.append({
                "t": t, "sym": r["sym"], "hour": t.hour, "dow": t.weekday(),
                "pos": r["pos"], "rsi": r["rsi"], "dist": r["dist"],
                "ret1": r["ret1"], "ret5": r["ret5"], "ret24": r["ret24"],
                "atrr": r["atrr"], "h4tr": np.sign(r["h4tr"]), "e200sl": np.sign(r["e200sl"]),
                "sw_lo": r["sw_lo"], "sw_hi": r["sw_hi"], "rng_w": r["rng_w"],
                "usdf": usdf if r["sym"] in USD_LONG else -usdf,
                "up": mv > 0})
    df = pd.DataFrame(rows)
    df["pair_id"] = df["sym"].astype("category").cat.codes
    return df

FEATS = ["hour","dow","pos","rsi","dist","ret1","ret5","ret24","atrr","h4tr","e200sl","sw_lo","sw_hi","rng_w","usdf","pair_id"]

def walk_forward(df, take_per_slot=1, thresh=0.0):
    df = df.sort_values("t").reset_index(drop=True)
    df["month"] = df["t"].astype(str).str[:7]
    months = sorted(df["month"].unique())
    test_months = months[-13:] if len(months) > 19 else months[6:]
    out = []
    for m in test_months:
        tr = df[df["month"] < m]
        te = df[df["month"] == m]
        if len(tr) < 2000 or not len(te): continue
        clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.06,
                                             l2_regularization=1.0, random_state=42)
        clf.fit(tr[FEATS], tr["up"])
        p = clf.predict_proba(te[FEATS])[:, 1]
        te = te.assign(p=p, conf=np.abs(p-0.5))
        for t, g in te.groupby("t"):
            g = g.sort_values("conf", ascending=False)
            for _, r in g.head(take_per_slot).iterrows():
                if r["conf"] < thresh: continue
                out.append({"t": t, "month": m, "win": (r["p"] > 0.5) == r["up"], "conf": r["conf"]})
    return pd.DataFrame(out)

def rep(s, name, days=261):
    mo = s.groupby("month")["win"].agg(["mean","size"])
    print(f"{name}: {100*s['win'].mean():.1f}% n={len(s)} ~{len(s)/days:.2f}/день мес≥65%: {int((mo['mean']>=0.65).sum())}/{len(mo)} худш {100*mo['mean'].min():.0f}%")
    for m, r in mo.iterrows(): print(f"  {m}: {100*r['mean']:.1f}% ({int(r['size'])})")

if __name__ == "__main__":
    df = build_dataset()
    print(f"dataset: {len(df)} samples, {df['t'].min()} .. {df['t'].max()}")
    res = walk_forward(df, take_per_slot=1)
    rep(res, "\nML walk-forward, топ-1/слот (~5/день)")
    # ~3/день: порог уверенности
    q = res["conf"].quantile(1 - 3/5)
    rep(res[res["conf"] >= q], f"\nML топ-1/слот, порог conf>={q:.3f} (~3/день)")
    rep(res[res["conf"] >= res["conf"].quantile(0.8)], "\nML только самые уверенные 20% (~1/день)")
    res.to_csv("ml_trades.csv", index=False)
