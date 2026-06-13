"""Download M1 history from histdata.com for all 28 pairs, aggregate to M15.
NOTE: histdata timestamps are EST (UTC-5, fixed, no DST) -> convert to UTC (+5h).
"""
import requests, re, io, zipfile, os, time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

PAIRS28 = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCHF","USDCAD","NZDUSD","EURJPY",
           "EURGBP","GBPJPY","AUDJPY","EURCHF","CHFJPY","CADJPY",
           "AUDCAD","AUDCHF","AUDNZD","CADCHF","EURAUD","EURCAD","EURNZD",
           "GBPAUD","GBPCAD","GBPCHF","GBPNZD","NZDCAD","NZDCHF","NZDJPY"]
MONTHS = ["2023", "2024"] + [f"{y}{m:02d}" for y in (2025,2026) for m in range(1,13)
          if f"{y}{m:02d}" <= "202606"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"
os.makedirs("m1_zips", exist_ok=True)

def grab(pair, ym):
    out = f"m1_zips/{pair}_{ym}.zip"
    if os.path.exists(out) and os.path.getsize(out) > 10000:
        return "cached"
    y = ym[:4]
    if len(ym) == 4:   # whole-year archive (completed years)
        url = f"https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair.lower()}/{y}"
    else:
        url = f"https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair.lower()}/{y}/{int(ym[4:])}"
    s = requests.Session(); s.headers.update({"User-Agent": UA})
    for attempt in range(3):
        try:
            r = s.get(url, timeout=30)
            tk = re.search(r'id="tk" value="([^"]+)"', r.text)
            if not tk:
                return "no-tk"
            r2 = s.post("https://www.histdata.com/get.php",
                        data={"tk": tk.group(1), "date": y, "datemonth": ym,
                              "platform": "ASCII", "timeframe": "M1", "fxpair": pair},
                        headers={"Referer": url}, timeout=90)
            if r2.status_code == 200 and len(r2.content) > 10000:
                with open(out, "wb") as f: f.write(r2.content)
                return "ok"
        except Exception:
            time.sleep(3)
    return "fail"

def download_all():
    jobs = [(p, ym) for p in PAIRS28 for ym in MONTHS]
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda j: (j, grab(*j)), jobs))
    bad = [(j, st) for j, st in res if st not in ("ok", "cached")]
    print(f"downloaded {len(res)-len(bad)}/{len(res)}; missing: {bad[:20]}", flush=True)

def build_m15():
    os.makedirs("data_m15", exist_ok=True)
    for pair in PAIRS28:
        frames = []
        for ym in MONTHS:
            fp = f"m1_zips/{pair}_{ym}.zip"
            if not os.path.exists(fp): continue
            try:
                z = zipfile.ZipFile(fp)
                name = [n for n in z.namelist() if n.endswith(".csv")][0]
                df = pd.read_csv(io.BytesIO(z.read(name)), sep=";", header=None,
                                 names=["dt","open","high","low","close","vol"])
                frames.append(df)
            except Exception as e:
                print(pair, ym, "parse fail", e, flush=True)
        if not frames:
            print(pair, "NO DATA", flush=True); continue
        m1 = pd.concat(frames, ignore_index=True)
        t = pd.to_datetime(m1["dt"], format="%Y%m%d %H%M%S")
        m1["time"] = t.dt.tz_localize("Etc/GMT+5").dt.tz_convert("UTC")  # EST fixed -> UTC
        m1 = m1.sort_values("time").set_index("time")
        g = pd.DataFrame({
            "open":  m1["open"].resample("15min", label="left", closed="left").first(),
            "high":  m1["high"].resample("15min", label="left", closed="left").max(),
            "low":   m1["low"].resample("15min", label="left", closed="left").min(),
            "close": m1["close"].resample("15min", label="left", closed="left").last(),
        }).dropna().reset_index()
        g.to_pickle(f"data_m15/{pair}.pkl")
        print(pair, len(g), g["time"].min(), "->", g["time"].max(), flush=True)

if __name__ == "__main__":
    download_all()
    build_m15()
