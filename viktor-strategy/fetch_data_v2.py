"""Fetch H1 data for candidate pairs from Yahoo Finance, cache to parquet."""
import pandas as pd, requests, time, os

PAIRS = ["EURUSD=X","GBPUSD=X","USDJPY=X","AUDUSD=X","USDCHF=X","USDCAD=X","NZDUSD=X","EURJPY=X",
         "EURGBP=X","GBPJPY=X","AUDJPY=X","EURCHF=X","CHFJPY=X","CADJPY=X"]

def fetch(symbol):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"interval":"60m","range":"730d"},
                     headers={"User-Agent":"Mozilla/5.0"}, timeout=60)
    res = r.json()["chart"]["result"][0]; q = res["indicators"]["quote"][0]
    return pd.DataFrame({"time": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                         "open": q["open"], "high": q["high"], "low": q["low"],
                         "close": q["close"]}).dropna().reset_index(drop=True)

os.makedirs("data", exist_ok=True)
for s in PAIRS:
    try:
        df = fetch(s)
        df.to_pickle(f"data/{s.replace('=X','')}.pkl")
        print(f"{s}: {len(df)} bars, {df['time'].min()} -> {df['time'].max()}")
    except Exception as e:
        print(f"{s}: FAILED {e}")
    time.sleep(1)
