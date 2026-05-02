"""
DERIV v22 — PER-COMBO RULES
============================
Each (pair × session) has its own optimized parameters, found via 10-day grid search.

Daily-stop logic:
- 2 wins → stop day
- 3 trades with 2 losses → escalate to 5 trades target 3 wins
- Hard cap 5 trades/day
"""
from __future__ import annotations
import os, sys, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, '/home/ubuntu/deriv_bot')
sys.path.insert(0, '/home/ubuntu/edge_backtest')
from deriv_v15pro_bot import Deriv, log_trade, LOG_DIR
from deriv_v17_pro import DERIV_SYMBOLS, in_session, SESSIONS
import logging

STAKE_USD = 50.0
MIN_TRADES_PER_DAY = 3
MAX_TRADES_PER_DAY = 5
WIN_TARGET_CLEAN = 2
WIN_TARGET_RECOVERY = 3
DEADLINE_HOUR = 22  # v22 trades all day to NY close

TOKEN = os.environ.get("DERIV_DEMO_TOKEN", "")
TRADES_CSV = LOG_DIR / "trades.csv"
DAILY_STATE = LOG_DIR / "v22_daily_state.json"
RULES_FILE = "/home/ubuntu/deriv_bot/v22_rules.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / f"v22_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("v22")

with open(RULES_FILE) as f:
    RULES_DATA = json.load(f)
RULES = RULES_DATA["rules"]
RULES_MAP = {(r["pair"], r["session"]): r for r in RULES}


def current_session(hour: int) -> str | None:
    for sn, (lo, hi) in SESSIONS.items():
        if lo <= hour <= hi:
            return sn
    return None


def load_daily_state() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    if DAILY_STATE.exists():
        s = json.load(open(DAILY_STATE))
        if s.get("date") == today:
            return s
    return {"date": today, "wins": 0, "losses": 0, "stopped": False,
            "open_trade_ids": [], "opened_total": 0, "max_trades": MIN_TRADES_PER_DAY,
            "target_wins": WIN_TARGET_CLEAN, "trade_pairs": []}


def save_daily_state(s: dict):
    DAILY_STATE.write_text(json.dumps(s, indent=2))


def update_results(deriv: Deriv, state: dict):
    if not state["open_trade_ids"]:
        return state
    still_open = []
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
                still_open.append(cid)
        except Exception as e:
            log.warning(f"check {cid}: {e}")
            still_open.append(cid)
    state["open_trade_ids"] = still_open
    return state


def should_stop(state: dict) -> tuple[bool, str]:
    w, l, total = state["wins"], state["losses"], state["opened_total"]
    if total >= MAX_TRADES_PER_DAY and not state["open_trade_ids"]:
        return True, f"hard cap {MAX_TRADES_PER_DAY}"
    if w >= state["target_wins"]:
        return True, f"target {state['target_wins']}W reached"
    return False, ""


def check_escalation(state: dict):
    """If 3 trades opened with 2L → escalate."""
    if state["opened_total"] == 3 and state["losses"] >= 2 and state["wins"] < 2:
        if state["max_trades"] != MAX_TRADES_PER_DAY:
            state["max_trades"] = MAX_TRADES_PER_DAY
            state["target_wins"] = WIN_TARGET_RECOVERY
            log.info(f"ESCALATE: 3 trades 2L → max={MAX_TRADES_PER_DAY} target=3W")


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Convert Deriv OHLC → indicators as in v22_grid_search."""
    # df from Deriv has Open/High/Low/Close (capitalized)
    d = pd.DataFrame({
        "open": df["Open"], "high": df["High"], "low": df["Low"], "close": df["Close"]
    }, index=df.index)
    c = d["close"]
    d["ema8"] = c.ewm(span=8, adjust=False).mean()
    d["ema21"] = c.ewm(span=21, adjust=False).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()

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

    d["mom5"] = c - c.shift(5)
    d["mom10"] = c - c.shift(10)

    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    bb_upper = ma20 + 2 * sd20
    bb_lower = ma20 - 2 * sd20
    d["bbp"] = (c - bb_lower) / (bb_upper - bb_lower).replace(0, np.nan)

    d["ema_score"] = (
        (d["ema8"] > d["ema21"]).astype(int)
        + (d["ema21"] > d["ema50"]).astype(int)
        - (d["ema8"] < d["ema21"]).astype(int)
        - (d["ema21"] < d["ema50"]).astype(int)
    )
    return d.dropna()


def signal_for_rule(df_ind: pd.DataFrame, rule: dict, sess: str) -> tuple[str, float] | None:
    """Check if latest bar triggers a signal per rule. Return (direction, score) or None."""
    if len(df_ind) == 0: return None
    row = df_ind.iloc[-1]
    if pd.isna(row["rsi"]) or pd.isna(row["atr"]) or row["atr"] == 0: return None

    p = rule["params"]
    macd_norm = row["macd_hist"] / row["atr"]

    if p["direction_mode"] == "trend":
        if (row["ema_score"] >= 2 and row["rsi"] > 50
                and macd_norm > p["min_macd_atr_ratio"] and row["mom5"] > 0):
            return ("BUY", float(row["close"]))
        if (row["ema_score"] <= -2 and row["rsi"] < 50
                and macd_norm < -p["min_macd_atr_ratio"] and row["mom5"] < 0):
            return ("SELL", float(row["close"]))
    elif p["direction_mode"] == "revert":
        if row["rsi"] < p["rsi_low"] and row["bbp"] < 0.1:
            return ("BUY", float(row["close"]))
        if row["rsi"] > p["rsi_high"] and row["bbp"] > 0.9:
            return ("SELL", float(row["close"]))
    return None


def find_candidates(deriv: Deriv) -> list:
    """Scan all rules valid for current session."""
    now = datetime.now(timezone.utc)
    sess = current_session(now.hour)
    if not sess:
        log.info(f"No active session at hour {now.hour}")
        return []

    candidates = []
    for rule in RULES:
        if rule["session"] != sess: continue
        p = rule["pair"]
        if p not in DERIV_SYMBOLS: continue
        try:
            df15 = deriv.get_candles(DERIV_SYMBOLS[p], 900, 200)
            df_ind = calc_indicators(df15)
            sig = signal_for_rule(df_ind, rule, sess)
            if sig:
                direction, entry = sig
                expiry_h = rule["expiry_min"] // 60
                candidates.append({
                    "rule": rule, "pair": p, "session": sess,
                    "direction": direction, "entry": entry,
                    "expiry_h": expiry_h, "expected_wr": rule["wr"],
                    "n_hist": rule["n"],
                })
        except Exception as e:
            log.exception(f"{p}: {e}")
    # Rank by expected WR descending, then n_hist
    candidates.sort(key=lambda c: (-c["expected_wr"], -c["n_hist"]))
    return candidates


def open_trade(deriv: Deriv, c: dict, state: dict):
    p = c["pair"]; rule = c["rule"]
    try:
        b = deriv.buy_contract(DERIV_SYMBOLS[p], c["direction"], c["expiry_h"], STAKE_USD)
        cid = b.get("contract_id")
        log.info(f"OPENED {p} {c['direction']} ${STAKE_USD} {c['expiry_h']}h cid={cid} (expWR {c['expected_wr']:.0f}%)")
        state["open_trade_ids"].append(cid)
        state["opened_total"] += 1
        state["trade_pairs"].append(p)
        log_trade({
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "pair": p, "deriv_symbol": DERIV_SYMBOLS[p],
            "direction": c["direction"], "expiry_h": c["expiry_h"],
            "score": c["expected_wr"], "v14_conf": 0, "old_conf": 0,
            "stake": STAKE_USD,
            "contract_id": cid,
            "buy_price": b.get("buy_price"),
            "payout": b.get("payout"),
            "dry_run": False, "mode": "v22_per_combo",
            "session": c["session"], "expected_wr": c["expected_wr"],
        })
    except Exception as e:
        log.exception(f"buy {p}: {e}")


def main():
    deriv = Deriv(TOKEN); deriv.connect(); deriv.authorize()
    log.info(f"Authorized virtual={deriv.is_virtual} balance={deriv.balance:.2f}")

    state = load_daily_state()
    state = update_results(deriv, state)
    check_escalation(state)
    save_daily_state(state)

    log.info(f"Daily {state['date']}: opened={state['opened_total']} W={state['wins']} L={state['losses']} "
             f"max={state['max_trades']} target={state['target_wins']}W")

    stop, reason = should_stop(state)
    if stop:
        log.info(f"STOP DAY: {reason}")
        deriv.close()
        return

    if state["opened_total"] >= state["max_trades"]:
        log.info(f"Reached max_trades {state['max_trades']}, waiting for results")
        deriv.close()
        return

    now_utc = datetime.now(timezone.utc)
    if now_utc.hour >= DEADLINE_HOUR:
        log.info(f"Past deadline {DEADLINE_HOUR}:00 UTC")
        deriv.close()
        return

    cands = find_candidates(deriv)
    log.info(f"Candidates this session: {len(cands)}")

    # Open up to (max_trades - opened_total) but only 1 at a time per run (15-min cron)
    slots = state["max_trades"] - state["opened_total"]
    if slots <= 0:
        deriv.close()
        return

    # Avoid trading same pair twice in one day
    cands = [c for c in cands if c["pair"] not in state.get("trade_pairs", [])]

    if not cands:
        log.info("No candidates available (or already traded today)")
        deriv.close()
        return

    # Open ONE best candidate per run
    best = cands[0]
    log.info(f"BEST: {best['pair']} {best['session']} {best['direction']} expWR={best['expected_wr']:.0f}% N={best['n_hist']}")
    open_trade(deriv, best, state)
    save_daily_state(state)
    deriv.close()


if __name__ == "__main__":
    main()
