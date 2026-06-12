"""Build a per-(pair, hour-bar) features + 3h-outcome table. No lookahead:
all features come from bars <= t; outcome = close[i+3] vs close[i]."""
import pandas as pd, numpy as np, os

PAIRS14 = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCHF","USDCAD","NZDUSD","EURJPY",
           "EURGBP","GBPJPY","AUDJPY","EURCHF","CHFJPY","CADJPY"]

def rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100/(1 + up/dn.replace(0, np.nan))

def ema(s, n): return s.ewm(span=n, adjust=False).mean()

def prep(sym):
    h = pd.read_pickle(f"data/{sym}.pkl").copy()
    c = h["close"]
    h["rsi"] = rsi(c)
    h["hi20"] = h["high"].rolling(20).max(); h["lo20"] = h["low"].rolling(20).min()
    h["pos"] = (c - h["lo20"]) / (h["hi20"] - h["lo20"])
    tr = pd.concat([h["high"]-h["low"], (h["high"]-c.shift()).abs(), (h["low"]-c.shift()).abs()], axis=1).max(axis=1)
    h["atr"] = tr.rolling(14).mean()
    h["atrp"] = h["atr"].rolling(500).rank(pct=True)        # vol regime percentile
    h["atr_rel"] = h["atr"] / h["atr"].rolling(200).median() # rel to median (golden zone 1.1-1.4)
    h["dist"] = (c - ema(c, 50)) / h["atr"]
    h["ret5"] = c.pct_change(5)
    h["ret24"] = c.pct_change(24)
    h["e200sl"] = ema(c, 200).diff(20)
    h["h4tr"] = ema(c, 200).diff(4)
    # 24h sweep: previous 24h high/low taken out then price back inside
    hi24 = h["high"].shift(1).rolling(24).max(); lo24 = h["low"].shift(1).rolling(24).min()
    h["sweep_lo"] = ((h["low"] < lo24) & (c > lo24)).astype(int)
    h["sweep_hi"] = ((h["high"] > hi24) & (c < hi24)).astype(int)
    # candle body direction of last bar
    h["body"] = (c - h["open"]) / h["atr"]
    h["sym"] = sym
    return h

def build(horizon=3):
    rows = []
    for sym in PAIRS14:
        h = prep(sym)
        c = h["close"].values
        n = len(h)
        fut = np.full(n, np.nan)
        fut[:n-horizon] = c[horizon:] - c[:n-horizon]
        h = h.assign(move=fut)
        h = h.iloc[550:]                       # warmup for rolling windows
        h = h[h["move"].notna() & (h["move"] != 0)]
        h = h[h["time"].dt.weekday < 5]
        rows.append(h)
    df = pd.concat(rows, ignore_index=True)
    df["hour"] = df["time"].dt.hour
    df["dow"] = df["time"].dt.weekday
    df["month"] = df["time"].dt.strftime("%Y-%m")
    return df

if __name__ == "__main__":
    df = build(3)
    df.to_pickle("candidates_h3.pkl")
    print(len(df), "rows", df["time"].min(), "->", df["time"].max())
    print(df.groupby("sym").size())
