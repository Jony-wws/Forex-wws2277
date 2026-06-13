"""V12 = V11 (28 pairs, H4 screen) + M15 microstructure + D1 screen.
Causality convention: features of H1 row with bar-start t are used for an entry
at t+1h (close of bar t), same as all rolling features on bars <= t.
- M15: only candles fully CLOSED by t+1h (start <= t+45m, label=left).
- D1:  only fully completed days (day end <= t+1h).
"""
import pandas as pd, numpy as np
from features import rsi, ema
from features_v11 import PAIRS28, prep as prep_v11

M15F = ["m15_ret4","m15_ret16","m15_rsi","m15_macd","m15_bbz","m15_body","m15_accel"]
D1F  = ["d1_rsi","d1_dist","d1_pos","d1_ret5","d1_prev_range","d1_prev_dir"]

def m15_features(h, sym):
    try:
        g = pd.read_pickle(f"data_m15/{sym}.pkl").sort_values("time")
    except FileNotFoundError:
        for col in M15F: h[col] = np.nan
        return h
    c = g["close"]
    tr = pd.concat([g["high"]-g["low"], (g["high"]-c.shift()).abs(),
                    (g["low"]-c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    f = pd.DataFrame({"time": g["time"]})
    f["m15_ret4"] = c.pct_change(4)
    f["m15_ret16"] = c.pct_change(16)
    f["m15_rsi"] = rsi(c, 14)
    f["m15_macd"] = (ema(c,12) - ema(c,26)) / atr
    ma20 = c.rolling(20).mean(); sd20 = c.rolling(20).std()
    f["m15_bbz"] = (c - ma20) / sd20
    f["m15_body"] = (c - g["open"]) / atr
    f["m15_accel"] = f["m15_ret4"].diff(4)
    # candle with start time s is closed at s+15m; usable for entry t+1h if s <= t+45m
    key = (h["time"] + pd.Timedelta(minutes=45)).rename("key")
    out = pd.merge_asof(pd.DataFrame({"key": key}).sort_values("key"),
                        f.sort_values("time"), left_on="key", right_on="time",
                        direction="backward", tolerance=pd.Timedelta(hours=24))
    out = out.set_index(key.index)
    for col in M15F: h[col] = out[col]
    return h

def d1_features(h):
    x = h.set_index("time")
    o = x["open"].resample("1D", label="left", closed="left").first()
    hi = x["high"].resample("1D", label="left", closed="left").max()
    lo = x["low"].resample("1D", label="left", closed="left").min()
    c = x["close"].resample("1D", label="left", closed="left").last()
    g = pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c}).dropna()
    tr = pd.concat([g["high"]-g["low"], (g["high"]-g["close"].shift()).abs(),
                    (g["low"]-g["close"].shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    f = pd.DataFrame(index=g.index)
    f["d1_rsi"] = rsi(g["close"], 14)
    f["d1_dist"] = (g["close"] - ema(g["close"], 20)) / atr
    hi20 = g["high"].rolling(20).max(); lo20 = g["low"].rolling(20).min()
    f["d1_pos"] = (g["close"] - lo20) / (hi20 - lo20)
    f["d1_ret5"] = g["close"].pct_change(5)
    f["d1_prev_range"] = (g["high"] - g["low"]) / atr
    f["d1_prev_dir"] = np.sign(g["close"] - g["open"])
    # day starting at index d is completed at d+1d
    f = f.reset_index().rename(columns={"time": "dstart"})
    f["done"] = f["dstart"] + pd.Timedelta(days=1)
    key = (h["time"] + pd.Timedelta(hours=1)).rename("key")
    out = pd.merge_asof(pd.DataFrame({"key": key}).sort_values("key"),
                        f.sort_values("done"), left_on="key", right_on="done",
                        direction="backward")
    out = out.set_index(key.index)
    for col in D1F: h[col] = out[col]
    return h

def prep(sym):
    h = prep_v11(sym)
    h = m15_features(h, sym)
    h = d1_features(h)
    # full multi-timeframe agreement: M15 + H1 + H4 momentum same direction
    h["mtf_all_agree"] = ((np.sign(h["m15_ret16"]) == np.sign(h["ret24"])) &
                          (np.sign(h["ret24"]) == np.sign(h["h4_ret"]))).astype(int)
    return h

def build(horizon=3):
    rows = []
    for sym in PAIRS28:
        h = prep(sym)
        c = h["close"].values; n = len(h)
        fut = np.full(n, np.nan); fut[:n-horizon] = c[horizon:] - c[:n-horizon]
        h = h.assign(move=fut).iloc[550:]
        h = h[h["move"].notna() & (h["move"] != 0)]
        h = h[h["time"].dt.weekday < 5]
        rows.append(h)
        print(sym, "ok", flush=True)
    df = pd.concat(rows, ignore_index=True)
    df["hour"] = df["time"].dt.hour; df["dow"] = df["time"].dt.weekday
    df["month"] = df["time"].dt.strftime("%Y-%m")
    return df

if __name__ == "__main__":
    df = build(3)
    df.to_pickle("candidates_v12.pkl")
    miss = df[M15F[0]].isna().mean()
    print(len(df), "rows", df["time"].min(), "->", df["time"].max(), f"m15 missing: {100*miss:.1f}%")
