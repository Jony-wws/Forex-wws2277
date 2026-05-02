"""
DERIV v26 — v25 + News blackout + DXY filter + S/R levels.

Free APIs only:
- ForexFactory RSS for high-impact news (±30 min blackout)
- Yahoo Finance DXY for USD-pair direction filter
- Pivot points for S/R from data we already have

Daily: same as v25 (min 3, max 15, stop at WR≥70% AND W-L≥2)
"""
from __future__ import annotations
import os, sys, json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, '/home/ubuntu/deriv_bot')
sys.path.insert(0, '/home/ubuntu/edge_backtest')

from deriv_v15pro_bot import Deriv, log_trade, LOG_DIR
from deriv_v17_pro import DERIV_SYMBOLS
from news_dxy_filter import is_news_blackout, dxy_aligned, calc_pivot, near_sr, get_dxy_trend
import logging

STAKE_USD = 50.0
MIN_TRADES_PER_DAY = 3
MAX_TRADES_PER_DAY = 15
WR_TARGET = 0.70

SESSIONS = {"Asia": (0, 6), "London": (7, 11), "LON+NY": (12, 15), "NY": (16, 21)}
TOKEN = os.environ.get("DERIV_DEMO_TOKEN", "")
DAILY_STATE = LOG_DIR / "v26_daily_state.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / f"v26_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("v26")

V24 = json.load(open("/home/ubuntu/deriv_bot/v24_rules.json"))["rules"]
V23 = json.load(open("/home/ubuntu/deriv_bot/v23_rules.json"))["rules"]
V23 = [r for r in V23 if r["wr"] >= 70 and r["n"] >= 30]
for r in V24: r["src"] = "v24"; r["priority"] = 1
for r in V23: r["src"] = "v23"; r["priority"] = 2
ALL_RULES = V24 + V23


def current_session(hour: int) -> str | None:
    for sn, (lo, hi) in SESSIONS.items():
        if lo <= hour <= hi: return sn
    return None


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = pd.DataFrame({"open": df["Open"], "high": df["High"], "low": df["Low"], "close": df["Close"]}, index=df.index)
    c = d["close"]
    d["ema8"] = c.ewm(span=8, adjust=False).mean()
    d["ema21"] = c.ewm(span=21, adjust=False).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()
    d["ema200"] = c.ewm(span=200, adjust=False).mean()
    delta = c.diff()
    up = delta.where(delta > 0, 0).rolling(14).mean()
    dn = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = up / dn.replace(0, np.nan)
    d["rsi"] = 100 - 100 / (1 + rs)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    d["macd_hist"] = macd - sig
    tr = pd.concat([d["high"]-d["low"], (d["high"]-c.shift()).abs(),
                    (d["low"]-c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    d["bbp"] = (c - (ma20 - 2*sd20)) / ((ma20 + 2*sd20) - (ma20 - 2*sd20))
    d["dist_ema200_atr"] = (c - d["ema200"]) / d["atr"]
    win = min(480, max(50, len(d)//2))
    d["atr_pct"] = d["atr"].rolling(win).rank(pct=True)
    low14 = d["low"].rolling(14).min()
    high14 = d["high"].rolling(14).max()
    d["stoch"] = 100 * (c - low14) / (high14 - low14).replace(0, np.nan)
    d["mom5"] = c - c.shift(5)
    d["macd_norm"] = d["macd_hist"] / d["atr"]
    d["ema_score"] = (
        (d["ema8"] > d["ema21"]).astype(int)
        + (d["ema21"] > d["ema50"]).astype(int)
        + (d["ema50"] > d["ema200"]).astype(int)
        - (d["ema8"] < d["ema21"]).astype(int)
        - (d["ema21"] < d["ema50"]).astype(int)
        - (d["ema50"] < d["ema200"]).astype(int)
    )
    return d.dropna()


def signal_v24(df, params):
    if len(df) == 0: return None
    row = df.iloc[-1]
    if any(pd.isna(row[k]) for k in ["rsi","bbp","stoch","atr_pct","dist_ema200_atr"]): return None
    p = params
    if (row["rsi"] < p["rsi_lo"] and row["bbp"] < p["bbp_lo"] and row["stoch"] < p["stoch_lo"]
        and row["dist_ema200_atr"] > -p["dist_max"] and row["atr_pct"] < p["atr_max"]):
        return ("BUY", float(row["close"]))
    if (row["rsi"] > p["rsi_hi"] and row["bbp"] > p["bbp_hi"] and row["stoch"] > p["stoch_hi"]
        and row["dist_ema200_atr"] < p["dist_max"] and row["atr_pct"] < p["atr_max"]):
        return ("SELL", float(row["close"]))
    return None


def signal_v23(df, params):
    if len(df) == 0: return None
    row = df.iloc[-1]
    if any(pd.isna(row[k]) for k in ["rsi","bbp","macd_norm","mom5","ema_score"]): return None
    if params["direction_mode"] == "trend":
        if (row["ema_score"] >= params["min_ema_score"] and row["rsi"] > 50
            and row["macd_norm"] > params["min_macd_atr_ratio"] and row["mom5"] > 0):
            return ("BUY", float(row["close"]))
        if (row["ema_score"] <= -params["min_ema_score"] and row["rsi"] < 50
            and row["macd_norm"] < -params["min_macd_atr_ratio"] and row["mom5"] < 0):
            return ("SELL", float(row["close"]))
    elif params["direction_mode"] == "revert":
        if row["rsi"] < params["rsi_low"] and row["bbp"] < params["bbp_low"]:
            return ("BUY", float(row["close"]))
        if row["rsi"] > params["rsi_high"] and row["bbp"] > params["bbp_high"]:
            return ("SELL", float(row["close"]))
    return None


def passes_filters(pair: str, direction: str, price: float, df_15m: pd.DataFrame, now_utc: datetime) -> tuple[bool, list[str]]:
    """Apply news, DXY, S/R filters. Return (passes, reasons_list)."""
    reasons = []

    # 1. News blackout
    blacked, msg = is_news_blackout(pair, now_utc)
    if blacked:
        return False, [f"FILTER FAIL: {msg}"]
    reasons.append("news: ok")

    # 2. DXY alignment (USD pairs only)
    aligned, msg = dxy_aligned(pair, direction)
    if not aligned:
        return False, [f"FILTER FAIL DXY: {msg}"]
    reasons.append(f"DXY: {msg}")

    # 3. S/R proximity (don't trade right at a strong level — wait for break/bounce confirmation)
    df_pd = pd.DataFrame({
        "Open": df_15m.get("open", df_15m.get("Open")),
        "High": df_15m.get("high", df_15m.get("High")),
        "Low": df_15m.get("low", df_15m.get("Low")),
        "Close": df_15m.get("close", df_15m.get("Close")),
    }, index=df_15m.index)
    sr = calc_pivot(df_pd)
    if sr:
        is_near, lvl = near_sr(price, sr, threshold_pct=0.03)
        if is_near:
            return False, [f"FILTER FAIL S/R: too close to {lvl}"]
        reasons.append(f"S/R: ok (pivot {sr['pivot']:.4f})")

    return True, reasons


# State management
def load_state() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    if DAILY_STATE.exists():
        s = json.load(open(DAILY_STATE))
        if s.get("date") == today: return s
    return {"date": today, "wins": 0, "losses": 0, "open_trade_ids": [],
            "opened_total": 0, "stopped": False, "executed_signatures": []}


def save_state(s: dict): DAILY_STATE.write_text(json.dumps(s, indent=2))


def update_results(deriv: Deriv, state: dict):
    if not state["open_trade_ids"]: return state
    still = []
    for cid in state["open_trade_ids"]:
        try:
            r = deriv.call({"proposal_open_contract": 1, "contract_id": cid})
            poc = r.get("proposal_open_contract", {})
            if poc.get("is_sold") == 1 or poc.get("status") in ("won", "lost"):
                profit = float(poc.get("profit", 0))
                if profit > 0:
                    state["wins"] += 1
                    log.info(f"  WIN +${profit:.2f} cid={cid}")
                else:
                    state["losses"] += 1
                    log.info(f"  LOSS ${profit:.2f} cid={cid}")
            else:
                still.append(cid)
        except Exception as e:
            log.warning(f"check {cid}: {e}")
            still.append(cid)
    state["open_trade_ids"] = still
    return state


def should_stop(state: dict) -> tuple[bool, str]:
    n = state["opened_total"]
    w = state["wins"]; l = state["losses"]
    if n >= MAX_TRADES_PER_DAY and not state["open_trade_ids"]:
        return True, f"hard cap {MAX_TRADES_PER_DAY}"
    if n >= MIN_TRADES_PER_DAY:
        if state["open_trade_ids"]: return False, ""
        wr = w / n
        if w >= 2 and wr >= WR_TARGET and (w - l) >= 2:
            return True, f"target met: {w}W/{l}L wr={wr*100:.0f}%"
    return False, ""


def find_candidates(deriv: Deriv) -> list:
    now = datetime.now(timezone.utc)
    sess = current_session(now.hour)
    if not sess:
        log.info("No active session"); return []
    cands = []
    rejected_filter = []
    for r in ALL_RULES:
        if r["session"] != sess: continue
        p = r["pair"]
        if p not in DERIV_SYMBOLS: continue
        try:
            df15 = deriv.get_candles(DERIV_SYMBOLS[p], 900, 3500)
            df_ind = calc_indicators(df15)
            if len(df_ind) < 50: continue
            sig = signal_v24(df_ind, r["params"]) if r["src"] == "v24" else signal_v23(df_ind, r["params"])
            if sig:
                direction, entry = sig
                # Apply filters
                ok, reasons = passes_filters(p, direction, entry, df_ind, now)
                if not ok:
                    rejected_filter.append((p, direction, reasons[0]))
                    continue
                exp_h = max(1, r["expiry_min"] // 60)
                cands.append({"rule": r, "pair": p, "session": sess,
                              "direction": direction, "entry": entry,
                              "expiry_h": exp_h, "expected_wr": r["wr"],
                              "src": r["src"], "priority": r["priority"],
                              "reasons": reasons})
        except Exception as e:
            log.exception(f"{p}: {e}")
    if rejected_filter:
        log.info(f"Filtered out: {len(rejected_filter)}")
        for p, d, msg in rejected_filter[:5]:
            log.info(f"  {p} {d}: {msg}")
    cands.sort(key=lambda c: (c["priority"], -c["expected_wr"]))
    return cands


def open_trade(deriv: Deriv, c: dict, state: dict):
    p = c["pair"]
    try:
        b = deriv.buy_contract(DERIV_SYMBOLS[p], c["direction"], c["expiry_h"], STAKE_USD)
        cid = b.get("contract_id")
        log.info(f"OPENED {p} {c['session']} {c['direction']} ${STAKE_USD} {c['expiry_h']}h "
                 f"src={c['src']} expWR={c['expected_wr']:.0f}% cid={cid}")
        log.info(f"  Filter checks: {' | '.join(c['reasons'])}")
        state["open_trade_ids"].append(cid)
        state["opened_total"] += 1
        sig = f"{p}_{c['session']}_{c['direction']}_{datetime.now(timezone.utc).strftime('%H%M')[:3]}"
        state["executed_signatures"].append(sig)
        log_trade({
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "pair": p, "deriv_symbol": DERIV_SYMBOLS[p],
            "direction": c["direction"], "expiry_h": c["expiry_h"],
            "score": c["expected_wr"], "v14_conf": 0, "old_conf": 0,
            "stake": STAKE_USD, "contract_id": cid,
            "buy_price": b.get("buy_price"), "payout": b.get("payout"),
            "dry_run": False, "mode": f"v26_{c['src']}",
            "session": c["session"], "expected_wr": c["expected_wr"],
        })
    except Exception as e:
        log.exception(f"buy {p}: {e}")


def main():
    deriv = Deriv(TOKEN); deriv.connect(); deriv.authorize()
    log.info(f"Authorized virtual={deriv.is_virtual} balance=${deriv.balance:.2f}")
    state = load_state()
    state = update_results(deriv, state)
    save_state(state)
    log.info(f"Daily {state['date']}: opened={state['opened_total']} W={state['wins']} L={state['losses']} open={len(state['open_trade_ids'])}")
    stop, reason = should_stop(state)
    if stop:
        log.info(f"STOP: {reason}"); deriv.close(); return
    if state["opened_total"] >= MAX_TRADES_PER_DAY:
        log.info("Hard cap"); deriv.close(); return
    if len(state["open_trade_ids"]) >= 3:
        log.info(f"Already {len(state['open_trade_ids'])} open, waiting"); deriv.close(); return

    dxy = get_dxy_trend()
    if dxy:
        log.info(f"DXY: {dxy['current']:.2f} ({dxy['change_24h_pct']:+.2f}%) trend={dxy['trend']}")

    cands = find_candidates(deriv)
    log.info(f"Candidates passing all filters: {len(cands)}")
    if not cands:
        log.info("No setups found this session"); deriv.close(); return
    cands = [c for c in cands
             if f"{c['pair']}_{c['session']}_{c['direction']}_{datetime.now(timezone.utc).strftime('%H%M')[:3]}"
             not in state.get("executed_signatures", [])]
    if not cands:
        log.info("All candidates already executed"); deriv.close(); return
    best = cands[0]
    log.info(f"BEST: {best['pair']} {best['session']} {best['direction']} src={best['src']} expWR={best['expected_wr']:.0f}%")
    open_trade(deriv, best, state)
    save_state(state)
    deriv.close()


if __name__ == "__main__":
    main()
