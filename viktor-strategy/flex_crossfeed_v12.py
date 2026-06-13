import pandas as pd, numpy as np, glob
import ml_v12 as M
big = pd.concat([pd.read_pickle(f) for f in sorted(glob.glob("ckpt_v12/*.pkl"))])
b = big[big["sym"]!="CADCHF"].copy()
b["conf"]=(b["P_champv11"]-0.5).abs(); b["dir"]=np.where(b["P_champv11"]>0.5,1,-1); b["date"]=b["time"].dt.date
trades=[]; hist=[]
for day in sorted(b["date"].unique()):
    g=b[b["date"]==day]
    hb=g.sort_values("conf",ascending=False).groupby("hour").head(1).sort_values("hour")
    sc=[r for _,r in hb.iterrows() if not M.news_hit(r["time"])]
    tau=np.quantile(hist,0.80) if len(hist)>=60 else None
    rem=3
    for i,r in enumerate(sc):
        if rem==0: break
        forced=(len(sc)-i-1)<rem
        if forced or (tau is None and r["hour"] in M.slots) or (tau is not None and r["conf"]>=tau):
            trades.append({"time":r["time"],"sym":r["sym"],"dir":r["dir"],"month":r["month"],"win":bool((r["move"]>0)==(r["dir"]>0))}); rem-=1
    hist.extend([r["conf"] for r in sc])
t=pd.DataFrame(trades)
mo=t.groupby("month")["win"].agg(["mean","size"])
print(f"E-noCADCHF (Yahoo feed): {100*t['win'].mean():.1f}% n={len(t)} mes>=65: {int((mo['mean']>=0.65).sum())}/{len(mo)} worst: {100*mo['mean'].min():.1f}%")
for mm,row in mo.iterrows(): print(f"  {mm}: {100*row['mean']:.1f}% ({int(row['size'])})")
# cross-feed check on histdata
m15={}
def px(sym, ts):
    ts = ts.tz_localize(None) if ts.tzinfo is not None else ts
    if sym not in m15:
        try: m15[sym]=pd.read_pickle(f"data_m15_fixed/{sym}.pkl").sort_values("time").reset_index(drop=True)
        except FileNotFoundError: m15[sym]=None
    g=m15[sym]
    if g is None: return np.nan
    i=g["time"].searchsorted(ts - pd.Timedelta(minutes=15), side="right")-1
    if i<0: return np.nan
    if (ts - g["time"].iloc[i]) > pd.Timedelta(hours=2): return np.nan
    return g["close"].iloc[i]
res=[]
for _,r in t.iterrows():
    e=px(r["sym"], r["time"]+pd.Timedelta(hours=1)); x=px(r["sym"], r["time"]+pd.Timedelta(hours=4))
    res.append(np.nan if (np.isnan(e) or np.isnan(x) or e==x) else bool((x-e>0)==(r["dir"]>0)))
t["win_hd"]=res
v=t.dropna(subset=["win_hd"])
mo2=v.groupby("month")["win_hd"].agg(["mean","size"])
print(f"\nE-noCADCHF (histdata feed): {100*v['win_hd'].mean():.1f}% n={len(v)} mes>=65: {int((mo2['mean']>=0.65).sum())}/{len(mo2)} worst: {100*mo2['mean'].min():.1f}%")
for mm,row in mo2.iterrows(): print(f"  {mm}: {100*row['mean']:.1f}% ({int(row['size'])})")
