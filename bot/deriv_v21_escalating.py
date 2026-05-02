"""
DERIV v21 — ESCALATING DAILY-STOP STRATEGY
============================================
Guarantees minimum 3 trades/day by escalating filter strictness over time.

TIER 1 (UTC 00:00-12:00): STRICT v19 — best setups, WR 75-80%
TIER 2 (UTC 12:00-16:00): MEDIUM — relaxed score, WR 65-72%
TIER 3 (UTC 16:00-19:00): FORCE — top by score, WR 55-65%

Daily stop logic preserved:
  - 2W clean → stop day
  - 3W with any losses → stop day
  - 5 trades hard cap
"""
from __future__ import annotations
import os, sys, json
from datetime import datetime, timezone
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

# v21 PARAMS
STAKE_USD = 50.0
MIN_TRADES_PER_DAY = 3
MAX_TRADES_PER_DAY = 5
WIN_TARGET_CLEAN = 2
WIN_TARGET_RECOVERY = 3

# Tier boundaries (UTC hours)
TIER_1_END_HOUR = 12  # before this: STRICT
TIER_2_END_HOUR = 16  # before this: MEDIUM
DEADLINE_HOUR = 19    # after this: too late to open new (1h before user 00:00)

TOKEN = os.environ.get("DERIV_DEMO_TOKEN", "")
TRADES_CSV = LOG_DIR / "trades.csv"
DAILY_STATE = LOG_DIR / "v21_daily_state.json"
RULES_FILE = "/home/ubuntu/deriv_bot/v19_rules.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / f"v21_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("v21")

with open(RULES_FILE) as f:
    PRIMARY_RULES = json.load(f)["primary"]


def get_tier(hour_utc: int) -> str:
    if hour_utc < TIER_1_END_HOUR: return "STRICT"
    if hour_utc < TIER_2_END_HOUR: return "MEDIUM"
    if hour_utc < DEADLINE_HOUR: return "FORCE"
    return "DEADLINE_PASSED"


def load_daily_state() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    if DAILY_STATE.exists():
        s = json.load(open(DAILY_STATE))
        if s.get("date") == today:
            return s
    return {"date": today, "wins": 0, "losses": 0, "stopped": False,
            "open_trade_ids": [], "opened_total": 0}


def save_daily_state(s: dict):
    DAILY_STATE.write_text(json.dumps(s, indent=2))


def update_results_from_deriv(deriv: Deriv, state: dict):
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
    total = state["opened_total"]
    if total >= MAX_TRADES_PER_DAY and not state["open_trade_ids"]:
        return True, f"hard cap {MAX_TRADES_PER_DAY} trades"
    if total >= MIN_TRADES_PER_DAY:
        # Daily target met, can stop on win conditions
        if l == 0 and w >= WIN_TARGET_CLEAN:
            return True, f"clean {WIN_TARGET_CLEAN}W stop"
        if l >= 1 and w >= WIN_TARGET_RECOVERY:
            return True, f"recovery {WIN_TARGET_RECOVERY}W stop"
    return False, ""


def find_candidates(deriv: Deriv, tier: str, ctx) -> list:
    """Find triggered candidates given the current tier."""
    relevant = sorted(set(r["pair"] for r in PRIMARY_RULES))
    if tier == "FORCE":
        # In force mode, scan ALL pairs
        relevant = list(DERIV_SYMBOLS.keys())

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

    if tier == "STRICT":
        # Apply v19 rules normally
        for r in PRIMARY_RULES:
            p = r["pair"]; sess = r["session"]
            if p not in pair_data: continue
            c = pair_data[p]
            if not in_session(c["ts"].hour, sess): continue
            if abs(c["score"]) < r["min_score"]: continue
            if c["vc"] < r["min_vc"] or c["oc"] < r["min_oc"]: continue
            if is_liquidity_warmup(now_utc, sess): continue
            blocked, reason = is_blackout_for_pair(p, now_utc)
            if blocked: continue
            storm, _, _ = is_volatility_storm(c["df_1h"])
            if storm: continue
            direction = "BUY" if c["score"] > 0 else "SELL"
            if not trend_aligned(c["df_4h"], direction): continue
            if already_traded_recently(p, sess): continue
            triggered.append({"rule": r, "pair": p, "session": sess,
                              "direction": direction, "score": c["score"],
                              "vc": c["vc"], "oc": c["oc"],
                              "expiry_h": r["expiry_h"], "expected_wr": r["wr"],
                              "tier": tier})
    elif tier == "MEDIUM":
        # Relax: drop trend alignment, lower score by 4
        for r in PRIMARY_RULES:
            p = r["pair"]; sess = r["session"]
            if p not in pair_data: continue
            c = pair_data[p]
            if not in_session(c["ts"].hour, sess): continue
            relaxed_min_score = max(14, r["min_score"] - 4)
            if abs(c["score"]) < relaxed_min_score: continue
            if c["vc"] < max(0, r["min_vc"]-1) or c["oc"] < max(0, r["min_oc"]-1): continue
            blocked, reason = is_blackout_for_pair(p, now_utc)
            if blocked: continue
            direction = "BUY" if c["score"] > 0 else "SELL"
            if already_traded_recently(p, sess): continue
            triggered.append({"rule": r, "pair": p, "session": sess,
                              "direction": direction, "score": c["score"],
                              "vc": c["vc"], "oc": c["oc"],
                              "expiry_h": r["expiry_h"],
                              "expected_wr": r["wr"] - 5,  # downgrade expected WR
                              "tier": tier})
    elif tier == "FORCE":
        # Last resort: take top by abs(score) >= 10, any pair, 2h expiry
        for p, c in pair_data.items():
            if abs(c["score"]) < 10: continue
            blocked, reason = is_blackout_for_pair(p, now_utc)
            if blocked: continue
            direction = "BUY" if c["score"] > 0 else "SELL"
            triggered.append({"rule": None, "pair": p, "session": "FORCE",
                              "direction": direction, "score": c["score"],
                              "vc": c["vc"], "oc": c["oc"],
                              "expiry_h": 2,
                              "expected_wr": 60.0,  # baseline assumption
                              "tier": tier})

    triggered.sort(key=lambda x: -abs(x["score"]) if x["tier"]=="FORCE" else -x["expected_wr"])
    return triggered


def run_once():
    if not TOKEN:
        log.error("No DERIV_DEMO_TOKEN env var"); return
    deriv = Deriv(TOKEN); deriv.connect(); deriv.authorize()
    if not deriv.is_virtual:
        log.error("REFUSING: not virtual"); deriv.close(); return

    now_utc = datetime.now(timezone.utc)
    tier = get_tier(now_utc.hour)
    state = load_daily_state()
    log.info(f"Daily state {state['date']} (UTC {now_utc.hour:02d}:{now_utc.minute:02d} TIER={tier}): "
             f"opened={state['opened_total']} wins={state['wins']} losses={state['losses']} "
             f"open={len(state['open_trade_ids'])} stopped={state['stopped']}")

    state = update_results_from_deriv(deriv, state)
    save_daily_state(state)

    stop, reason = should_stop_day(state)
    if stop:
        if not state["stopped"]:
            log.info(f"=== STOPPING DAY: {reason} ===")
            log.info(f"Final: {state['wins']}W/{state['losses']}L  P&L≈${state['wins']*39.5 - state['losses']*50:.2f}")
            state["stopped"] = True
            save_daily_state(state)
        deriv.close(); return state

    if tier == "DEADLINE_PASSED":
        log.info(f"Past deadline UTC {DEADLINE_HOUR}:00 — no more opens today")
        deriv.close(); return state

    if state["open_trade_ids"]:
        log.info(f"Waiting for {len(state['open_trade_ids'])} open to close before next")
        deriv.close(); return state

    log.info("Loading cross-asset ctx...")
    ctx = download_context_assets("3mo")

    triggered = find_candidates(deriv, tier, ctx)
    log.info(f"Triggered in {tier} tier: {len(triggered)}")

    if not triggered:
        log.info(f"No setup in {tier}. Will retry next run.")
        deriv.close(); return state

    t = triggered[0]
    log.info(f"OPENING [{tier}] {t['pair']} {t['direction']} {t['expiry_h']}h "
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
            "dry_run": False, "mode": f"v21_{tier.lower()}",
            "session": t["session"], "expected_wr": t["expected_wr"],
        })
        state["open_trade_ids"].append(cid)
        state["opened_total"] = state.get("opened_total", 0) + 1
        save_daily_state(state)
        log.info(f"  Opened cid={cid} payout=${buy.get('payout'):.2f}")
    except Exception as e:
        log.exception(f"Open failed: {e}")

    deriv.close()
    return state


if __name__ == "__main__":
    run_once()
