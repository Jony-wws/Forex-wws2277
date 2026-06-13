"""Rebuild data_m15 with America/New_York DST-aware conversion (histdata = NY local time).
Evidence: market close always 17:00 their-time year-round => NY local, not fixed EST."""
import pandas as pd, io, zipfile, os
from fetch_m15 import PAIRS28, MONTHS
os.makedirs("data_m15_fixed", exist_ok=True)
for pair in PAIRS28:
    frames=[]
    for ym in MONTHS:
        fp=f"m1_zips/{pair}_{ym}.zip"
        if not os.path.exists(fp): continue
        try:
            z=zipfile.ZipFile(fp); name=[n for n in z.namelist() if n.endswith(".csv")][0]
            frames.append(pd.read_csv(io.BytesIO(z.read(name)), sep=";", header=None,
                          names=["dt","open","high","low","close","vol"]))
        except Exception as e: print(pair, ym, "fail", e, flush=True)
    if not frames: print(pair,"NO DATA",flush=True); continue
    m1=pd.concat(frames, ignore_index=True)
    t=pd.to_datetime(m1["dt"], format="%Y%m%d %H%M%S")
    m1["time"]=t.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")
    m1=m1.dropna(subset=["time"]).sort_values("time").set_index("time")
    g=pd.DataFrame({
        "open": m1["open"].resample("15min",label="left",closed="left").first(),
        "high": m1["high"].resample("15min",label="left",closed="left").max(),
        "low":  m1["low"].resample("15min",label="left",closed="left").min(),
        "close":m1["close"].resample("15min",label="left",closed="left").last(),
    }).dropna().reset_index()
    g["time"]=g["time"].dt.tz_localize(None)
    g.to_pickle(f"data_m15_fixed/{pair}.pkl")
    print(pair, len(g), flush=True)
