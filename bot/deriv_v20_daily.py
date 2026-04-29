"""
DERIV v20 — DAILY-STOP STRATEGY
================================
Built on top of v19 universal rules.
DAILY logic:
  - Each day starts fresh
  - After 2 wins (no losses) → STOP for the day (lock profit)
  - After 3 wins (with any losses) → STOP for the day
  - Hard cap 5 trades/day (prevent runaway)
  - Once stopped, no more trades that day even if signals appear

Also runs autonomously via scheduled cron.
Stake: $50/trade.
Backtest: 51/74 days (68.9%) hit ≥70% WR. Aggregate 72.89% WR. Avg +$34/day.
"""
from __future__ import annotations
import os, sys, json
from datetime import datetime, timezone, date
from pathlib import Path
import pandas as pd
sys.path.insert(0, '/home/ubuntu/deriv_bot')
sys.path.insert(0, '/home/ubuntu/edge_backtest')
from deriv_v15pro_bot import Deriv, get_15m_1h_4h, log_trade, LOG_DIR
from edge_v14 import score_v14
from edge_v13 import download_context_assets
from news_filter import is_blackout_for_pair, upcoming_high_impact
from deriv_v17_pro import (
    is_volatility_storm, trend_aligned, is_liquidity_warmup,
    in_session, SESSIONS, DERIV_SYMBOLS, PAIR_YF, already_traded_recently
)
import logging

# v20 PARAMS
STAKE_USD = 50.0
MAX_TRADES_PER_DAY = 5
WIN_TARGET_CLEAN = 2  # 2W with no losses → stop
WIN_TARGET_RECOVERY = 3  # 3W with any losses → stop

TOKEN = os.environ.get("DERIV_DEMO_TOKEN", "")
TRADES_CSV = LOG_DIR / "trades.csv"
DAILY_STATE = LOG_DIR / "v20_daily_state.json"
RULES_FILE = "/home/ubuntu/deriv_bot/v19_rules.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / f"v20_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("v20")

with open(RULES_FILE) as f:
    PRIMARY_RULES = json.load(f)["primary"]


def load_daily_state() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    if DAILY_STATE.exists():
        s = json.load(open(DAILY_STATE))
        if s.get("date") == today:
            return s
    return {"date": today, "wins": 0, "losses": 0, "stopped": False, "open_trade_ids": []}


def save_daily_state(s: dict):
    DAILY_STATE.write_text(json.dumps(s, indent=2))


def update_results_from_deriv(deriv: Deriv, state: dict):
    """Check our v20 trades opened today, count W/L of those that have closed."""
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
                    log.info(f"  ✓ Closed contract {cid}: WIN +${profit:.2f}")
                else:
                    state["losses"] += 1
                    log.info(f"  ✗ Closed contract {cid}: LOSS ${profit:.2f}")
            else:
                still_open.append(cid)
        except Exception as e:
            log.warning(f"Could not check contract {cid}: {e}")
            still_open.append(cid)
    state["open_trade_ids"] = still_open
    return state


def should_stop_day(state: dict) -> tuple[bool, str]:
    w, l = state["wins"], state["losses"]
    if w + l >= MAX_TRADES_PER_DAY and not state["open_trade_ids"]:
        return True, f"hard cap {MAX_TRADES_PER_DAY} trades reached"
    if l == 0 and w >= WIN_TARGET_CLEAN:
        return True, f"clean win target {WIN_TARGET_CLEAN}W reached"
    if l >= 1 and w >= WIN_TARGET_RECOVERY:
        return True, f"recovery target {WIN_TARGET_RECOVERY}W after losses reached"
    return False, ""


def run_once():
    if not TOKEN:
        log.error("No DERIV_DEMO_TOKEN env var"); return
    deriv = Deriv(TOKEN); deriv.connect(); deriv.authorize()
    if not deriv.is_virtual:
        log.error("REFUSING: not virtual"); deriv.close(); return

    state = load_daily_state()
    log.info(f"Daily state {state['date']}: wins={state['wins']} losses={state['losses']} "
             f"open={len(state['open_trade_ids'])} stopped={state['stopped']}")

    # 1. Update from Deriv: check open trades for closure
    state = update_results_from_deriv(deriv, state)
    save_daily_state(state)

    # 2. Check stop conditions
    stop, reason = should_stop_day(state)
    if stop:
        if not state["stopped"]:
            log.info(f"=== STOPPING DAY: {reason} ===")
            log.info(f"Final tally: {state['wins']}W / {state['losses']}L (P&L approx ${state['wins']*39.5 - state['losses']*50:.2f})")
            state["stopped"] = True
            save_daily_state(state)
        else:
            log.info(f"Already stopped: {reason}")
        deriv.close(); return state

    # 3. Don't open if we have open ones — wait for them to settle (so stop logic works)
    if state["open_trade_ids"]:
        log.info(f"Waiting for {len(state['open_trade_ids'])} open trades to close before next open")
        deriv.close(); return state

    # 4. Look for new trade
    log.info("Loading cross-asset ctx...")
    ctx = download_context_assets("3mo")

    relevant = sorted(set(r["pair"] for r in PRIMARY_RULES))
    pair_data = {}
    for p in relevant:
        try:
            d15, d1h, d4h = get_15m_1h_4h(deriv, DERIV_SYMBOLS[p])
            sc = score_v14(d15, d1h, d4h, PAIR_YF[p], ctx).dropna()
            if len(sc) == 0: continue
            last = sc.iloc[-1]
            pair_data[p] = {"score": float(last["score_v14"]),
                            "vc": int(last["v14_confluence"]),
                            "oc": int(last["confluence"]),
                            "ts": sc.index[-1],
                            "df_1h": d1h, "df_4h": d4h}
        except Exception as e:
            log.exception(f"{p} score error: {e}")

    triggered = []
    now_utc = datetime.now(timezone.utc)
    for r in PRIMARY_RULES:
        p = r["pair"]; sess = r["session"]
        if p not in pair_data: continue
        c = pair_data[p]
        if not in_session(c["ts"].hour, sess): continue
        if abs(c["score"]) < r["min_score"]: continue
        if c["vc"] < r["min_vc"] or c["oc"] < r["min_oc"]: continue
        if is_liquidity_warmup(now_utc, sess): continue
        blocked, reason = is_blackout_for_pair(p, now_utc)
        if blocked: log.info(f"SKIP {p}: {reason}"); continue
        storm, _, _ = is_volatility_storm(c["df_1h"])
        if storm: continue
        direction = "BUY" if c["score"] > 0 else "SELL"
        if not trend_aligned(c["df_4h"], direction): continue
        if already_traded_recently(p, sess): continue

        triggered.append({"rule": r, "pair": p, "session": sess,
                          "direction": direction, "score": c["score"],
                          "vc": c["vc"], "oc": c["oc"],
                          "expiry_h": r["expiry_h"], "expected_wr": r["wr"]})

    triggered.sort(key=lambda x: -x["expected_wr"])
    log.info(f"Triggered after all filters: {len(triggered)}")

    if not triggered:
        log.info("No setup matches v19 right now. Will retry next run.")
        deriv.close(); return state

    # 5. Open ONE trade (best WR) — strategy is sequential, not parallel
    t = triggered[0]
    log.info(f"OPENING {t['pair']} {t['direction']} {t['expiry_h']}h "
             f"(score={t['score']:+.1f} expected WR={t['expected_wr']:.1f}%)")
    try:
        buy = deriv.buy_contract(DERIV_SYMBOLS[t["pair"]], t["direction"],
                                  t["expiry_h"], STAKE_USD)
        cid = buy.get("contract_id")
        log_trade({
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "pair": t["pair"], "deriv_symbol": DERIV_SYMBOLS[t["pair"]],
            "direction": t["direction"], "expiry_h": t["expiry_h"],
            "score": t["score"], "v14_conf": t["vc"], "old_conf": t["oc"],
            "stake": STAKE_USD,
            "contract_id": cid,
            "buy_price": buy.get("buy_price"),
            "payout": buy.get("payout"),
            "dry_run": False, "mode": "v20_daily",
            "session": t["session"], "expected_wr": t["expected_wr"],
        })
        state["open_trade_ids"].append(cid)
        save_daily_state(state)
        log.info(f"  Opened cid={cid} payout=${buy.get('payout'):.2f}")
    except Exception as e:
        log.exception(f"Open failed: {e}")

    deriv.close()
    return state


if __name__ == "__main__":
    run_once()
