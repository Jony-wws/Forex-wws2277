"""ML v11 per JONY's rules (12.06.2026):
- ALL 28 forex pairs (currencies only), top-1 best pair per entry
- exactly 3 forecasts per 24h, but at ANY hours: scan every hour, take the
  3 most confident moments (causal threshold from PAST days + forced fill)
- multi-timeframe screen H4 added to features (M15 history unavailable >60d)
- 3h expiry, news filter kept
- roster: fired recency/volregime/liquidity; hired MTF specialist;
  proven path = new features go DIRECTLY to champion (no committee voting)
Walk-forward, honest (train < test month).
"""
import pandas as pd, numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from news_calendar import red_events

DF = pd.read_pickle("candidates_v12.pkl").sort_values("time").reset_index(drop=True)

# --- currency strength factors for all 8 currencies from all 28 pairs ---
CURS = ["USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD"]
for cur in CURS:
    sub = DF[DF["sym"].str.contains(cur)].copy()
    sgn = np.where(sub["sym"].str[:3] == cur, 1.0, -1.0)
    sub["r"] = sgn * sub["ret24"]
    DF = DF.merge(sub.groupby("time")["r"].mean().rename(f"f_{cur.lower()}"),
                  on="time", how="left")
FACTORS = [f"f_{c.lower()}" for c in CURS]

DF = pd.concat([DF, pd.get_dummies(DF["sym"], prefix="p")], axis=1)
PAIRS = [c for c in DF.columns if c.startswith("p_")]
DF["up"] = (DF["move"] > 0).astype(int)

BASE = ["pos","rsi","dist","ret5","ret24","atrp","atr_rel","e200sl","h4tr","sweep_lo","sweep_hi","body","hour","dow"]
FULL = BASE + ["f_usd"] + PAIRS                     # champion V6b feature set
H4F  = ["h4_rsi","h4_dist","h4_pos","h4_macd","h4_slope","h4_ret","mtf_trend_agree","mtf_mom_agree","mtf_rsi_gap"]
M15F = ["m15_ret4","m15_ret16","m15_rsi","m15_macd","m15_bbz","m15_body","m15_accel"]
D1F  = ["d1_rsi","d1_dist","d1_pos","d1_ret5","d1_prev_range","d1_prev_dir","mtf_all_agree"]
CHV11 = FULL + H4F + M15F + D1F + [f for f in FACTORS if f != "f_usd"]   # champion + new instruments

MOM   = ["ret1","ret3","ret5","ret12","ret24","ret48","ret120","macd","e200sl","h4tr","hour","dow"] + PAIRS
REV   = ["pos","rsi","rsi5","rsi28","bbz","dist","dist100","atrp","body","hour"] + PAIRS
SESS  = ["sess","day_open_dist","day_range","hour","dow","pos","ret5","body"] + PAIRS
CAL   = ["dom","wom","dow","hour","pos","rsi","ret24","e200sl"] + PAIRS
STREN = FACTORS + ["ret24","e200sl","h4tr","pos","hour","dow"] + PAIRS
MTF   = H4F + M15F + D1F + ["pos","rsi","dist","ret24","atrp","hour","dow"] + PAIRS
ALLF  = sorted(set(CHV11 + MOM + REV + SESS + CAL + STREN + MTF))
num = [c for c in ALLF if not c.startswith("p_")]
DF[num] = DF[num].replace([np.inf,-np.inf], np.nan)

end = DF["time"].max() - pd.Timedelta(hours=6)
start = end - pd.Timedelta(days=365)
months = sorted(DF[(DF["time"]>=start)&(DF["time"]<=end)]["month"].unique())
EVENTS = red_events()
slots = (12,16,20)
DEEP = dict(max_iter=250, max_depth=6, learning_rate=0.05, min_samples_leaf=120)
PAIRS14 = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCHF","USDCAD","NZDUSD","EURJPY",
           "EURGBP","GBPJPY","AUDJPY","EURCHF","CHFJPY","CADJPY"]

def news_hit(t):
    return any(t - pd.Timedelta(minutes=30) <= e <= t + pd.Timedelta(hours=3) for e in EVENTS)

SPECS = [  # (name, feats, n_seeds)
    ("champion",  FULL,  7),   # V6b core, old eyes (H1 only)
    ("champv11",  CHV11, 7),   # champion + MTF/strength instruments
    ("mtf",       MTF,   1),   # new hire: multi-timeframe specialist
    ("momentum",  MOM,   1),
    ("meanrev",   REV,   1),
    ("session",   SESS,  1),
    ("calendar",  CAL,   1),
    ("strength",  STREN, 1),
    ("widefull",  ALLF,  1),
]

def main():
    import os
    os.makedirs("ckpt_v12", exist_ok=True)
    rows = []
    for m in months:
        ck = f"ckpt_v12/{m}.pkl"
        if os.path.exists(ck):
            rows.append(pd.read_pickle(ck))
            print(f"month {m} loaded from checkpoint", flush=True)
            continue
        test = DF[(DF["month"]==m) & (DF["time"]>=start) & (DF["time"]<=end)].copy()  # ALL hours
        train = DF[DF["month"] < m]
        if len(train) < 5000 or test.empty: continue
        for name, feats, nseeds in SPECS:
            ps = []
            for sd in range(nseeds):
                clf = HistGradientBoostingClassifier(random_state=sd, **DEEP)
                clf.fit(train[feats].astype(float), train["up"])
                ps.append(clf.predict_proba(test[feats].astype(float))[:,1])
            test[f"P_{name}"] = np.mean(ps, axis=0)
        test.to_pickle(ck)
        rows.append(test)
        print(f"month {m} done", flush=True)
    big = pd.concat(rows)
    big.to_pickle("mlv12_probs.pkl")
    evaluate(big)

def report(tr, label, save=None):
    tr = pd.DataFrame(tr)
    mo = tr.groupby("month")["win"].agg(["mean","size"])
    print(f"{label}: {100*tr['win'].mean():.1f}% n={len(tr)} мес≥65%: {int((mo['mean']>=0.65).sum())}/{len(mo)} худший: {100*mo['mean'].min():.1f}%", flush=True)
    for mm, row in mo.iterrows():
        print(f"  {mm}: {100*row['mean']:.1f}% ({int(row['size'])})", flush=True)
    if save: tr.to_pickle(save)

def sim_fixed(big, pcol, label, syms=None):
    """Old system: fixed slots 12/16/20 UTC, news shifts entry +1h."""
    b = big if syms is None else big[big["sym"].isin(syms)]
    b = b.copy(); b["conf"] = (b[pcol]-0.5).abs(); b["dir"] = np.where(b[pcol]>0.5, 1, -1)
    trades = []
    for day in b["time"].dt.date.unique():
        g_day = b[b["time"].dt.date == day]
        for s in slots:
            t_norm = g_day[g_day["hour"]==s]
            if t_norm.empty: continue
            use = t_norm
            if news_hit(t_norm["time"].iloc[0]):
                sh = g_day[g_day["hour"]==s+1]
                if not sh.empty: use = sh
            r = use.nlargest(1, "conf").iloc[0]
            trades.append({"t": r["time"], "sym": r["sym"], "win": bool((r["move"]>0)==(r["dir"]>0)), "month": r["month"]})
    report(trades, label)

def sim_flex(big, pcol, label, q=0.80, save=None):
    """V11: scan EVERY hour, exactly 3 entries/day at the most confident
    moments. Causal threshold = expanding quantile of PAST days' per-hour
    top-1 confidence; forced fill near day end guarantees 3/day."""
    b = big.copy(); b["conf"] = (b[pcol]-0.5).abs(); b["dir"] = np.where(b[pcol]>0.5, 1, -1)
    b["date"] = b["time"].dt.date
    trades = []; hist = []   # history of per-hour top-1 confidence (past days only)
    for day in sorted(b["date"].unique()):
        g_day = b[b["date"] == day]
        hour_best = (g_day.sort_values("conf", ascending=False)
                          .groupby("hour").head(1).sort_values("hour"))
        scannable = [r for _, r in hour_best.iterrows() if not news_hit(r["time"])]
        tau = np.quantile(hist, q) if len(hist) >= 60 else None
        remaining = 3
        for i, r in enumerate(scannable):
            if remaining == 0: break
            hours_left = len(scannable) - i - 1
            forced = hours_left < remaining          # must take to reach 3/day
            if forced or tau is None and r["hour"] in slots or (tau is not None and r["conf"] >= tau):
                trades.append({"t": r["time"], "sym": r["sym"], "forced": forced,
                               "win": bool((r["move"]>0)==(r["dir"]>0)), "month": r["month"]})
                remaining -= 1
        hist.extend([r["conf"] for r in scannable])  # update AFTER the day is over
    report(trades, label, save=save)

def evaluate(big):
    for name, _, _ in SPECS:
        pred = (big[f"P_{name}"] > 0.5) == (big["up"] == 1)
        print(f"solo {name}: {100*pred.mean():.1f}% (all rows)", flush=True)
    sim_fixed(big, "P_champion", "A) ЧЕМПИОН, 14 пар, фикс. часы (старая система)", syms=PAIRS14)
    sim_fixed(big, "P_champion", "B) ЧЕМПИОН, 28 пар, фикс. часы")
    sim_fixed(big, "P_champv11", "C) ЧЕМПИОН+МТФ(M15+H4+D1), 28 пар, фикс. часы")
    sim_flex(big, "P_champion", "D) ЧЕМПИОН, 28 пар, ГИБКИЕ часы")
    sim_flex(big, "P_champv11", "E) ЧЕМПИОН+МТФ(M15+H4+D1), 28 пар, ГИБКИЕ часы (V12)", save="mlv12_trades.pkl")

if __name__ == "__main__":
    main()
