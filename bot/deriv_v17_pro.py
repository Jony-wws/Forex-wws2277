"""
v17 PRO Deriv Trading Bot — "Думай как трейдер"
================================================
Strict v16 rules + pro-trader filters:
  1. News blackout (high-impact economic events)
  2. Volatility regime (ATR-based: skip if too volatile)
  3. Liquidity check (skip first 30 min after session open)
  4. Trend alignment (4H direction must match signal)
  5. Risk: max 5 parallel, dedupe per (pair, session) for 4h
"""
import os, sys, json, time, logging, websocket
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/ubuntu/edge_backtest')
sys.path.insert(0, '/home/ubuntu/deriv_bot')
from edge_v14 import score_v14
from edge_v13 import download_context_assets
from deriv_v15pro_bot import Deriv, get_15m_1h_4h, log_trade, LOG_DIR
from news_filter import is_blackout_for_pair, upcoming_high_impact

# Load v16 rules
with open("/home/ubuntu/deriv_bot/v19_rules.json") as f:
    rules_data = json.load(f)
PRIMARY_RULES = rules_data["primary"]

DERIV_SYMBOLS = {p: f"frx{p}" for p in
    ['USDJPY','USDCAD','USDCHF','AUDUSD','NZDUSD','EURUSD','GBPUSD',
     'EURJPY','GBPJPY','AUDJPY','CADJPY','EURGBP','EURCHF','EURAUD','GBPCHF']}
PAIR_YF = {p: f"{p}=X" for p in DERIV_SYMBOLS}

SESSIONS = {
    "Asia":   (0, 6),
    "London": (7, 11),
    "LON+NY": (12, 15),
    "NY":     (16, 21),
}

STAKE_USD = 1.0
MAX_OPEN_PARALLEL = 5
DEDUPE_WINDOW_H = 4
LIQUIDITY_BUFFER_MIN = 30   # skip first 30 min after session open
VOLATILITY_MULTIPLIER = 2.0  # skip if ATR > 2x median

TOKEN = os.environ.get("DERIV_DEMO_TOKEN", "")
TRADES_CSV = LOG_DIR / "trades.csv"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / f"v17_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("v17")


def in_session(hour_utc: int, sess_name: str) -> bool:
    lo, hi = SESSIONS[sess_name]
    return lo <= hour_utc <= hi


def is_liquidity_warmup(now_utc: datetime, sess_name: str) -> bool:
    """First 30 minutes after session open = thin liquidity, skip."""
    lo, _ = SESSIONS[sess_name]
    if now_utc.hour == lo and now_utc.minute < LIQUIDITY_BUFFER_MIN:
        return True
    return False


def calc_atr(df: pd.DataFrame, n: int = 14) -> float:
    """Average True Range on the last n bars."""
    h_l = df["High"] - df["Low"]
    h_pc = (df["High"] - df["Close"].shift(1)).abs()
    l_pc = (df["Low"] - df["Close"].shift(1)).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    return tr.tail(n).mean()


def is_volatility_storm(df_1h: pd.DataFrame) -> tuple[bool, float, float]:
    """Compare current ATR vs median of last 30 days. Storm if 2x+."""
    if len(df_1h) < 30 * 24:
        return False, 0, 0
    cur = calc_atr(df_1h.tail(14), 14)
    med = df_1h["High"].rolling(14).max() - df_1h["Low"].rolling(14).min()
    med_val = med.tail(30 * 24).median()
    if med_val <= 0:
        return False, cur, med_val
    ratio = cur / med_val
    return ratio > VOLATILITY_MULTIPLIER, cur, med_val


def trend_aligned(df_4h: pd.DataFrame, direction: str) -> bool:
    """Check if 4H trend (last 20 bars EMA slope) agrees with signal direction."""
    if len(df_4h) < 25: return True  # be permissive on insufficient data
    ema = df_4h["Close"].ewm(span=20, adjust=False).mean()
    slope = ema.iloc[-1] - ema.iloc[-5]
    if direction == "BUY":
        return slope > 0
    else:
        return slope < 0


def already_traded_recently(pair: str, session: str) -> bool:
    if not TRADES_CSV.exists(): return False
    df = pd.read_csv(TRADES_CSV)
    df = df[df["pair"] == pair]
    if "session" in df.columns:
        df = df[df["session"] == session]
    if df.empty: return False
    df["ts"] = pd.to_datetime(df["ts_utc"], errors="coerce", utc=True)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=DEDUPE_WINDOW_H)
    return (df["ts"] > cutoff).any()


def count_open_contracts(deriv: Deriv) -> int:
    resp = deriv.call({"portfolio": 1})
    return len(resp.get("portfolio", {}).get("contracts", []))


def run_once():
    if not TOKEN:
        log.error("No DERIV_DEMO_TOKEN env var"); return
    deriv = Deriv(TOKEN); deriv.connect(); deriv.authorize()
    if not deriv.is_virtual:
        log.error("REFUSING: not virtual"); deriv.close(); return

    open_count = count_open_contracts(deriv)
    log.info(f"Already-open contracts: {open_count}/{MAX_OPEN_PARALLEL}")

    # Show upcoming high-impact news (next 24h) for transparency
    upcoming = upcoming_high_impact(24)
    if upcoming:
        log.info(f"Upcoming high-impact news (next 24h): {len(upcoming)}")
        for e in upcoming[:5]:
            log.info(f"  +{e['minutes_from_now']:>4}min  {e['country']} {e['title']}")

    log.info("Loading cross-asset ctx...")
    ctx = download_context_assets("3mo")

    relevant_pairs = sorted(set(r["pair"] for r in PRIMARY_RULES))
    pair_data = {}
    for p in relevant_pairs:
        try:
            d15, d1h, d4h = get_15m_1h_4h(deriv, DERIV_SYMBOLS[p])
            sc = score_v14(d15, d1h, d4h, PAIR_YF[p], ctx).dropna()
            if len(sc) == 0: continue
            last = sc.iloc[-1]
            pair_data[p] = {
                "score": float(last["score_v14"]),
                "vc": int(last["v14_confluence"]),
                "oc": int(last["confluence"]),
                "ts": sc.index[-1],
                "df_1h": d1h, "df_4h": d4h,
            }
        except Exception as e:
            log.exception(f"{p} score error: {e}")

    triggered = []
    skipped_reasons = {"news":0, "volatility":0, "liquidity":0, "trend":0,
                       "session":0, "score":0, "confluence":0, "dedupe":0}

    now_utc = datetime.now(timezone.utc)
    for r in PRIMARY_RULES:
        p = r["pair"]; sess = r["session"]
        if p not in pair_data:
            continue
        c = pair_data[p]
        if not in_session(c["ts"].hour, sess):
            skipped_reasons["session"] += 1; continue
        if abs(c["score"]) < r["min_score"]:
            skipped_reasons["score"] += 1; continue
        if c["vc"] < r["min_vc"] or c["oc"] < r["min_oc"]:
            skipped_reasons["confluence"] += 1; continue
        # PRO FILTERS
        if is_liquidity_warmup(now_utc, sess):
            log.info(f"  SKIP {p} {sess}: liquidity warmup ({now_utc.minute} min into session)")
            skipped_reasons["liquidity"] += 1; continue
        blocked, reason = is_blackout_for_pair(p, now_utc)
        if blocked:
            log.info(f"  SKIP {p} {sess}: {reason}")
            skipped_reasons["news"] += 1; continue
        storm, atr_cur, atr_med = is_volatility_storm(c["df_1h"])
        if storm:
            log.info(f"  SKIP {p} {sess}: volatility storm (ATR cur={atr_cur:.5f} med={atr_med:.5f})")
            skipped_reasons["volatility"] += 1; continue
        direction = "BUY" if c["score"] > 0 else "SELL"
        if not trend_aligned(c["df_4h"], direction):
            log.info(f"  SKIP {p} {sess}: 4H trend disagrees with {direction}")
            skipped_reasons["trend"] += 1; continue
        if already_traded_recently(p, sess):
            skipped_reasons["dedupe"] += 1; continue

        triggered.append({"rule": r, "pair": p, "session": sess,
                          "direction": direction, "score": c["score"],
                          "vc": c["vc"], "oc": c["oc"],
                          "expiry_h": r["expiry_h"], "expected_wr": r["wr"]})

    log.info(f"\n=== Summary ===  skipped: {skipped_reasons}")
    log.info(f"Triggered (after all filters): {len(triggered)}")

    triggered.sort(key=lambda x: -x["expected_wr"])
    placed = []
    for t in triggered:
        if open_count + len(placed) >= MAX_OPEN_PARALLEL:
            log.info("MAX_OPEN_PARALLEL reached"); break
        try:
            buy = deriv.buy_contract(DERIV_SYMBOLS[t["pair"]], t["direction"], t["expiry_h"], STAKE_USD)
            log_trade({
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "pair": t["pair"], "deriv_symbol": DERIV_SYMBOLS[t["pair"]],
                "direction": t["direction"], "expiry_h": t["expiry_h"],
                "score": t["score"], "v14_conf": t["vc"], "old_conf": t["oc"],
                "stake": STAKE_USD,
                "contract_id": buy.get("contract_id"),
                "buy_price": buy.get("buy_price"),
                "payout": buy.get("payout"),
                "dry_run": False, "mode": "v17_pro",
                "session": t["session"], "expected_wr": t["expected_wr"],
            })
            placed.append(t)
            log.info(f"  >> PLACED {t['pair']} {t['direction']} {t['expiry_h']}h cid={buy.get('contract_id')}")
        except Exception as e:
            log.exception(f"  buy failed: {e}")

    bal = deriv.call({"balance": 1}).get("balance", {}).get("balance")
    log.info(f"Placed {len(placed)} new. Balance: ${bal}")
    deriv.close()
    return {"triggered": len(triggered), "placed": len(placed),
            "balance": bal, "skipped_reasons": skipped_reasons}


if __name__ == "__main__":
    res = run_once()
    print(f"\n=== Result ===\n{json.dumps(res, indent=2)}")
