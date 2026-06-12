"""ML v6: final push. (a) 7-seed ensemble + news; (b) deeper model 7-seed + news;
(c) 3-seed news with per-slot choice among top-2 conf pairs requiring vote agreement."""
import pandas as pd, numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from news_calendar import red_events

DF = pd.read_pickle("candidates_h3.pkl").sort_values("time").reset_index(drop=True)
FEATS = ["pos","rsi","dist","ret5","ret24","atrp","atr_rel","e200sl","h4tr","sweep_lo","sweep_hi","body","hour","dow"]
usd_sign = {"EURUSD":-1,"GBPUSD":-1,"AUDUSD":-1,"NZDUSD":-1,"USDJPY":1,"USDCHF":1,"USDCAD":1}
sub = DF[DF["sym"].isin(usd_sign)].copy()
sub["usd_ret"] = sub.apply(lambda r: usd_sign[r["sym"]]*r["ret24"], axis=1)
DF = DF.merge(sub.groupby("time")["usd_ret"].mean().rename("usd_factor"), on="time", how="left")
FEATS.append("usd_factor")
DF = pd.concat([DF, pd.get_dummies(DF["sym"], prefix="p")], axis=1)
FEATS += [c for c in DF.columns if c.startswith("p_")]
DF["up"] = (DF["move"] > 0).astype(int)

end = DF["time"].max() - pd.Timedelta(hours=6)
start = end - pd.Timedelta(days=365)
months = sorted(DF[(DF["time"]>=start)&(DF["time"]<=end)]["month"].unique())
EVENTS = red_events()
slots = (12,16,20)

def news_hit(t):
    return any(t - pd.Timedelta(minutes=30) <= e <= t + pd.Timedelta(hours=3) for e in EVENTS)

def run(label, seeds, params=None, save=None):
    params = params or dict(max_iter=150, max_depth=4, learning_rate=0.07, min_samples_leaf=200)
    all_trades = []
    for m in months:
        hours = set(slots) | {s+1 for s in slots}
        test = DF[(DF["month"]==m) & DF["hour"].isin(hours) & (DF["time"]>=start) & (DF["time"]<=end)].copy()
        train = DF[DF["month"] < m]
        if len(train) < 5000 or test.empty: continue
        X = train[FEATS].astype(float); y = train["up"]
        ps = []
        for sd in seeds:
            clf = HistGradientBoostingClassifier(random_state=sd, **params)
            clf.fit(X, y)
            ps.append(clf.predict_proba(test[FEATS].astype(float))[:,1])
        p = np.mean(ps, axis=0)
        test["p"] = p; test["conf"] = np.abs(p-0.5)
        test["dir"] = np.where(test["p"]>0.5, 1, -1)
        test["win"] = (test["move"]>0) == (test["dir"]>0)
        for day in test["time"].dt.date.unique():
            g_day = test[test["time"].dt.date == day]
            for s in slots:
                t_norm = g_day[g_day["hour"]==s]
                if t_norm.empty: continue
                use = t_norm
                if news_hit(t_norm["time"].iloc[0]):
                    sh = g_day[g_day["hour"]==s+1]
                    if not sh.empty: use = sh
                r = use.nlargest(1, "conf").iloc[0]
                all_trades.append({"t": r["time"], "sym": r["sym"], "win": bool(r["win"]), "month": r["month"]})
    tr = pd.DataFrame(all_trades)
    mo = tr.groupby("month")["win"].agg(["mean","size"])
    print(f"{label}: {100*tr['win'].mean():.1f}% n={len(tr)} мес≥65%: {int((mo['mean']>=0.65).sum())}/{len(mo)} худший: {100*mo['mean'].min():.1f}%", flush=True)
    if save: tr.to_pickle(save)

if __name__ == "__main__":
    run("V6a 7seed+news", seeds=range(7), save="mlv6a.pkl")
    run("V6b deep 7seed+news", seeds=range(7),
        params=dict(max_iter=250, max_depth=6, learning_rate=0.05, min_samples_leaf=120), save="mlv6b.pkl")
    run("V6c shallow 7seed+news", seeds=range(7),
        params=dict(max_iter=120, max_depth=3, learning_rate=0.08, min_samples_leaf=300), save="mlv6c.pkl")
