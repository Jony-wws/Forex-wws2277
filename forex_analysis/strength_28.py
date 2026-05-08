"""
Compute objective forex analysis for 28 pairs:
- D1, H4, H1 trends (EMA stack 8/21/55, EMA 200, HH/HL or LH/LL)
- ADX(14) on H4 (trend strength)
- ATR(14) on H4 (volatility)
- ADR(20) on D1 (% used today)
- 8-currency strength rank from D1 % change vs other 7
"""
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone

PAIRS = [
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD",
    "EURGBP", "EURAUD", "EURNZD", "EURJPY", "EURCHF", "EURCAD",
    "GBPAUD", "GBPNZD", "GBPJPY", "GBPCHF", "GBPCAD",
    "AUDNZD", "AUDJPY", "AUDCHF", "AUDCAD",
    "NZDJPY", "NZDCHF", "NZDCAD",
    "CADJPY", "CADCHF", "CHFJPY",
]
CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"]


def yfsym(p): return p + "=X"


def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def adx(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    up = h.diff(); dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean(), pdi, mdi


def trend_label(c, e8, e21, e55, e200):
    if e8 > e21 > e55 and c > e200:
        return "BUY"
    if e8 < e21 < e55 and c < e200:
        return "SELL"
    return "FLAT"


def fetch(pair, interval, period):
    df = yf.download(yfsym(pair), interval=interval, period=period, progress=False, auto_adjust=False, threads=False)
    if df.empty: return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def analyze_pair(pair):
    out = {"pair": pair}
    try:
        d1 = fetch(pair, "1d", "1y")
        h4 = fetch(pair, "1h", "60d")  # yfinance has no 4h, will resample
        h1 = fetch(pair, "1h", "30d")
        if d1 is None or len(d1) < 60 or h1 is None or len(h1) < 60:
            out["error"] = "insufficient data"
            return out

        # resample 1h -> 4h
        h4r = h1.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()

        # D1 trend
        c, e8, e21, e55, e200 = d1["Close"].iloc[-1], ema(d1["Close"], 8).iloc[-1], ema(d1["Close"], 21).iloc[-1], ema(d1["Close"], 55).iloc[-1], ema(d1["Close"], 200).iloc[-1]
        out["d1_close"] = float(c)
        out["d1_trend"] = trend_label(c, e8, e21, e55, e200)
        out["d1_above_200ema"] = bool(c > e200)

        # H4 trend + ADX
        c4, e84, e214, e554, e2004 = h4r["Close"].iloc[-1], ema(h4r["Close"], 8).iloc[-1], ema(h4r["Close"], 21).iloc[-1], ema(h4r["Close"], 55).iloc[-1], ema(h4r["Close"], 200).iloc[-1]
        out["h4_close"] = float(c4)
        out["h4_trend"] = trend_label(c4, e84, e214, e554, e2004)
        a4, p4, m4 = adx(h4r)
        out["h4_adx"] = float(a4.iloc[-1])
        out["h4_pdi"] = float(p4.iloc[-1])
        out["h4_mdi"] = float(m4.iloc[-1])

        # H1 trend
        c1, e81, e211, e551 = h1["Close"].iloc[-1], ema(h1["Close"], 8).iloc[-1], ema(h1["Close"], 21).iloc[-1], ema(h1["Close"], 55).iloc[-1]
        out["h1_close"] = float(c1)
        out["h1_trend"] = "BUY" if e81 > e211 > e551 else ("SELL" if e81 < e211 < e551 else "FLAT")

        # ATR H4 + ADR D1
        out["atr_h4"] = float(atr(h4r).iloc[-1])
        d_range = (d1["High"] - d1["Low"]).tail(20).mean()
        out["adr20"] = float(d_range)
        today = d1.iloc[-1]
        today_range = float(today["High"] - today["Low"])
        out["today_range"] = today_range
        out["adr_used_pct"] = round(today_range / float(d_range) * 100, 1) if d_range > 0 else 0

        # daily % change (for currency strength)
        if len(d1) >= 2:
            prev_close = float(d1["Close"].iloc[-2])
            out["d1_pct"] = round((float(c) - prev_close) / prev_close * 100, 3)
        else:
            out["d1_pct"] = 0.0

        # H4 confluence flag
        out["confluent"] = (out["d1_trend"] != "FLAT" and out["d1_trend"] == out["h4_trend"] == out["h1_trend"] and out["h4_adx"] > 25)

    except Exception as e:
        out["error"] = str(e)
    return out


def currency_strength(rows):
    """Use D1 % change of each pair to compute per-currency strength."""
    score = {c: 0.0 for c in CURRENCIES}
    cnt = {c: 0 for c in CURRENCIES}
    for r in rows:
        if "d1_pct" not in r: continue
        p = r["pair"]
        base, quote = p[:3], p[3:]
        # If base/quote pair goes up by X%, base gained X%, quote lost X%
        x = r["d1_pct"]
        score[base] += x; cnt[base] += 1
        score[quote] -= x; cnt[quote] += 1
    avg = {c: round(score[c] / max(cnt[c], 1), 3) for c in CURRENCIES}
    ranked = sorted(avg.items(), key=lambda kv: kv[1])  # weakest first
    rank = {c: i + 1 for i, (c, _) in enumerate(ranked)}
    return avg, rank


def main():
    print(f"Started {datetime.now(timezone.utc).isoformat()}")
    rows = []
    for p in PAIRS:
        r = analyze_pair(p)
        rows.append(r)
        if "error" in r:
            print(f"  {p}: ERR {r['error']}")
        else:
            print(f"  {p}: D1={r['d1_trend']:4s} H4={r['h4_trend']:4s} H1={r['h1_trend']:4s} ADX={r['h4_adx']:5.1f} ADRused={r['adr_used_pct']:5.1f}%  d1%={r['d1_pct']:+.3f}")

    print("\n=== CURRENCY STRENGTH (D1 % avg) ===")
    avg, rank = currency_strength(rows)
    for c, v in sorted(avg.items(), key=lambda kv: kv[1]):
        print(f"  {c}: rank={rank[c]} avg_d1%={v:+.3f}")

    print("\n=== CONFLUENT SIGNALS (D1=H4=H1, ADX>25) ===")
    for r in rows:
        if r.get("confluent"):
            p = r["pair"]; base, quote = p[:3], p[3:]
            gap = abs(rank[base] - rank[quote])
            print(f"  {p}: dir={r['d1_trend']} ADX={r['h4_adx']:.1f} ADRused={r['adr_used_pct']:.1f}% rank_gap={gap}")

    import json
    with open("/home/ubuntu/forex_analysis/result.json", "w") as f:
        json.dump({"rows": rows, "strength": avg, "rank": rank, "ts": datetime.now(timezone.utc).isoformat()}, f, default=str, indent=2)
    print("\nSaved /home/ubuntu/forex_analysis/result.json")


if __name__ == "__main__":
    main()
