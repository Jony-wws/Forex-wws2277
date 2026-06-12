#!/usr/bin/env python3
"""Разбор 10-11 июня 2026: как рынок двигался, где были ловушки,
и что случилось с двумя отправленными сигналами (SELL ~18:40 UTC 11.06,
BUY ~23:05 UTC 11.06). Запуск: uv run python analyze_2days.py"""
import os, sys
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def fetch(interval, rng, symbol="EURUSD=X"):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"interval": interval, "range": rng},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"time": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                       "open": q["open"], "high": q["high"],
                       "low": q["low"], "close": q["close"]}).dropna()
    return df.reset_index(drop=True)


def day_levels(d1, day):
    prev = d1[d1["time"].dt.date < day].tail(1)
    if prev.empty:
        return None
    return float(prev["high"].iloc[0]), float(prev["low"].iloc[0])


def main():
    h1 = fetch("60m", "10d")
    m15 = fetch("15m", "10d")
    d1 = fetch("1d", "1mo")

    for day_str in ["2026-06-10", "2026-06-11"]:
        day = datetime.fromisoformat(day_str).date()
        dd = h1[h1["time"].dt.date == day]
        if dd.empty:
            print(f"{day_str}: no data"); continue
        o = float(dd["open"].iloc[0]); c = float(dd["close"].iloc[-1])
        hi = float(dd["high"].max()); lo = float(dd["low"].min())
        hi_t = dd.loc[dd["high"].idxmax(), "time"]; lo_t = dd.loc[dd["low"].idxmin(), "time"]
        lv = day_levels(d1, day)
        print(f"\n===== {day_str} (UTC) =====")
        print(f"open {o:.5f}  close {c:.5f}  ({(c-o)/1e-4:+.0f} pips)  "
              f"high {hi:.5f} @{hi_t:%H:%M}  low {lo:.5f} @{lo_t:%H:%M}")
        if lv:
            pdh, pdl = lv
            print(f"prev day H/L: {pdh:.5f} / {pdl:.5f}  "
                  f"sweep_high={'YES' if hi > pdh and c < pdh else ('broke&held' if hi > pdh else 'no')}  "
                  f"sweep_low={'YES' if lo < pdl and c > pdl else ('broke&held' if lo < pdl else 'no')}")
        # hourly walk
        for _, r in dd.iterrows():
            bar = "↑" if r["close"] > r["open"] else "↓"
            print(f"  {r['time']:%H:%M} {bar} o{r['open']:.5f} h{r['high']:.5f} "
                  f"l{r['low']:.5f} c{r['close']:.5f} ({(r['close']-r['open'])/1e-4:+.0f}p)")

    # --- два сигнала 11.06 ---
    print("\n===== СИГНАЛЫ 11.06 =====")
    for label, t0 in [("SELL ~18:40 UTC", datetime(2026, 6, 11, 18, 40, tzinfo=timezone.utc)),
                      ("BUY  ~23:05 UTC", datetime(2026, 6, 11, 23, 5, tzinfo=timezone.utc))]:
        win = m15[(m15["time"] >= t0 - timedelta(minutes=15)) &
                  (m15["time"] <= t0 + timedelta(hours=5, minutes=15))]
        if win.empty:
            print(f"{label}: no m15 data"); continue
        entry = float(win["close"].iloc[0]); exit_ = float(win["close"].iloc[-1])
        hi = float(win["high"].max()); lo = float(win["low"].min())
        print(f"\n{label}: entry≈{entry:.5f} → expiry {exit_:.5f} ({(exit_-entry)/1e-4:+.0f} pips), "
              f"max +{(hi-entry)/1e-4:.0f}p / min {(lo-entry)/1e-4:.0f}p")
        for _, r in win[::2].iterrows():
            print(f"  {r['time']:%d %H:%M} c{r['close']:.5f} ({(r['close']-entry)/1e-4:+.0f}p)")


if __name__ == "__main__":
    main()
