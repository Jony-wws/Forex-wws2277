#!/usr/bin/env python3
"""365-day EUR/USD session study (Asia/London/NY) -> sessions.json
Data: Yahoo Finance H1 + D1 (same candles TradingView shows for FX).
Run: python analyze_sessions.py  (writes sessions.json next to itself)
"""
import json, os
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PIP = 0.0001

SESSIONS = {  # UTC hours [start, end)
    "asia":    {"hours": (0, 7),   "ru": "Азия (Токио/Сидней)"},
    "london":  {"hours": (7, 12),  "ru": "Лондон (открытие Европы)"},
    "overlap": {"hours": (12, 16), "ru": "Лондон+Нью-Йорк (перехлёст)"},
    "ny":      {"hours": (16, 21), "ru": "Нью-Йорк (вторая половина)"},
    "late":    {"hours": (21, 24), "ru": "Поздний вечер (тихие часы)"},
}

def fetch(interval, rng):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/EURUSD=X",
                     params={"interval": interval, "range": rng},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"time": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                       "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]}).dropna()
    return df.reset_index(drop=True)

def main():
    h1 = fetch("60m", "730d")
    d1 = fetch("1d", "2y")
    cutoff = h1["time"].max() - pd.Timedelta(days=365)
    h1 = h1[h1["time"] >= cutoff].reset_index(drop=True)
    print(f"H1 bars: {len(h1)}  span: {h1['time'].min()} .. {h1['time'].max()}")

    # daily trend (EMA50 vs EMA200 on D1, known at day start = use prev day)
    d1 = d1.sort_values("time").reset_index(drop=True)
    d1["ema50"] = d1["close"].ewm(span=50, adjust=False).mean()
    d1["ema200"] = d1["close"].ewm(span=200, adjust=False).mean()
    d1["trend"] = np.where(d1["ema50"] > d1["ema200"], 1, -1)
    trend_by_date = dict(zip(d1["time"].dt.date, d1["trend"].shift(1)))  # prev-day trend

    h1["date"] = h1["time"].dt.date
    h1["hour"] = h1["time"].dt.hour

    hourly = h1.groupby("hour").apply(
        lambda g: float((g["high"] - g["low"]).mean() / PIP), include_groups=False).round(1).to_dict()

    out = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
           "days_analyzed": 365, "pair": "EURUSD",
           "hourly_avg_range_pips": {str(k): v for k, v in sorted(hourly.items())},
           "sessions": {}}

    sess_dir = {}  # (date, sess) -> direction of session for momentum chain
    for key, meta in SESSIONS.items():
        a, b = meta["hours"]
        rows = []
        for date, g in h1[(h1["hour"] >= a) & (h1["hour"] < b)].groupby("date"):
            if len(g) < max(2, (b - a) - 2):
                continue
            o = float(g["open"].iloc[0]); c = float(g["close"].iloc[-1])
            hi = float(g["high"].max()); lo = float(g["low"].min())
            tr = trend_by_date.get(date)
            rows.append({"date": date, "dir": np.sign(c - o), "move": (c - o) / PIP,
                         "range": (hi - lo) / PIP, "trend": tr})
            sess_dir[(date, key)] = np.sign(c - o)
        df = pd.DataFrame(rows)
        with_t = df.dropna(subset=["trend"])
        follow = with_t[with_t["dir"] != 0]
        wins = (follow["dir"] == follow["trend"]).mean() * 100 if len(follow) else 50
        out["sessions"][key] = {
            "ru": meta["ru"], "utc_hours": list(meta["hours"]),
            "tk_hours": [(a + 5) % 24, (b + 5) % 24],
            "n_days": int(len(df)),
            "avg_range_pips": round(float(df["range"].mean()), 1),
            "median_abs_move_pips": round(float(df["move"].abs().median()), 1),
            "trend_follow_winrate_pct": round(float(wins), 1),
        }

    # momentum: does London follow Asia? Does NY follow overlap?
    pairs = [("asia", "london"), ("london", "overlap"), ("overlap", "ny")]
    out["session_momentum"] = {}
    dates = sorted({d for d, _ in sess_dir})
    for s1, s2 in pairs:
        same = tot = 0
        for d in dates:
            a, b2 = sess_dir.get((d, s1)), sess_dir.get((d, s2))
            if a in (1, -1) and b2 in (1, -1):
                tot += 1; same += (a == b2)
        out["session_momentum"][f"{s1}->{s2}"] = round(100 * same / tot, 1) if tot else None

    # confidence adjustments: rank sessions by trend_follow_winrate + range
    ranked = sorted(out["sessions"].items(), key=lambda kv: (-kv[1]["trend_follow_winrate_pct"], -kv[1]["avg_range_pips"]))
    adj_map = [2, 1, 0, -1, -2]
    for (k, v), adj in zip(ranked, adj_map):
        out["sessions"][k]["conf_adj"] = adj

    json.dump(out, open(os.path.join(HERE, "sessions.json"), "w"), ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
