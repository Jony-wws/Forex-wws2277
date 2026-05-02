"""
v15-PRO Deriv Trading Bot (DEMO ONLY)
=====================================
Auto-trades USDJPY, GBPJPY, EURUSD, EURCHF on Deriv demo account using
the v15-PRO Mode B rules validated on 113 days Dukascopy data.

Safety:
- Verifies account is_virtual==1 before every trade
- Refuses to trade if balance is real
- Logs every action to /home/ubuntu/deriv_bot/logs/
"""

import os
import sys
import json
import time
import asyncio
import logging
import websocket
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from pathlib import Path

# Add edge_backtest to path for v14 score logic
sys.path.insert(0, '/home/ubuntu/edge_backtest')
from edge_v14 import score_v14
from edge_v13 import download_context_assets

# ----- Config -----
TOKEN = os.environ.get("DERIV_DEMO_TOKEN", "")
APP_ID = "1089"
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

DERIV_SYMBOLS = {
    'USDJPY': 'frxUSDJPY',
    'GBPJPY': 'frxGBPJPY',
    'EURUSD': 'frxEURUSD',
    'EURCHF': 'frxEURCHF',
}

# Yahoo identifiers for cross-asset context (matches PAIR_YF in v15pro_backtest.py)
PAIR_YF = {
    'USDJPY': 'USDJPY=X',
    'GBPJPY': 'GBPJPY=X',
    'EURUSD': 'EURUSD=X',
    'EURCHF': 'EURCHF=X',
}

# Mode B trading rules per pair
RULES = {
    'USDJPY': {'session_utc': (0, 6),  'min_score': 30, 'min_oc': 4, 'min_vc': 0, 'expiry_h': 2},
    'GBPJPY': {'session_utc': (0, 6),  'min_score': 26, 'min_oc': 4, 'min_vc': 0, 'expiry_h': 3},
    'EURUSD': {'session_utc': (7, 16), 'min_score': 22, 'min_oc': 4, 'min_vc': 0, 'expiry_h': 3},
    'EURCHF': {'session_utc': (7, 16), 'min_score': 26, 'min_oc': 4, 'min_vc': 0, 'expiry_h': 2},
}

STAKE_USD = 1.0  # Conservative demo stake

# ----- Logging -----
LOG_DIR = Path("/home/ubuntu/deriv_bot/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
TRADES_CSV = LOG_DIR / "trades.csv"
RUN_LOG = LOG_DIR / f"bot_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(RUN_LOG),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("deriv_bot")


# ----- Deriv WebSocket helpers -----
class Deriv:
    def __init__(self, token: str):
        self.token = token
        self.ws = None
        self.req_id = 0
        self.is_virtual = None
        self.balance = None
        self.loginid = None

    def connect(self):
        self.ws = websocket.create_connection(WS_URL, timeout=30)
        self.ws.settimeout(30)
        log.info("Connected to Deriv WS")

    def call(self, payload: dict, timeout=30) -> dict:
        self.req_id += 1
        payload["req_id"] = self.req_id
        self.ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("req_id") == self.req_id:
                return data
        raise TimeoutError(f"No response for {payload}")

    def authorize(self):
        resp = self.call({"authorize": self.token})
        if "error" in resp:
            raise RuntimeError(f"Auth failed: {resp['error']}")
        a = resp["authorize"]
        self.is_virtual = a.get("is_virtual") == 1
        self.balance = a.get("balance")
        self.loginid = a.get("loginid")
        log.info(f"Authorized {self.loginid} virtual={self.is_virtual} balance={self.balance}")
        if not self.is_virtual:
            raise RuntimeError(f"REFUSING: account {self.loginid} is REAL, not virtual")
        return a

    def get_candles(self, symbol: str, granularity: int, count: int = 200) -> pd.DataFrame:
        """granularity in seconds: 60=1m, 900=15m, 3600=1H, 14400=4H"""
        resp = self.call({
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "granularity": granularity,
            "style": "candles",
        })
        if "error" in resp:
            raise RuntimeError(f"candles error {symbol}: {resp['error']}")
        candles = resp["candles"]
        df = pd.DataFrame(candles)
        df["time"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_localize(None)
        df = df.set_index("time")[["open", "high", "low", "close"]].astype(float)
        # Edge_v10 expects capitalized columns
        df.columns = ["Open", "High", "Low", "Close"]
        df["Volume"] = 0.0  # Deriv doesn't expose FX volume
        return df

    def buy_contract(self, symbol: str, direction: str, expiry_h: int, stake: float, dry_run: bool = False):
        """direction: 'CALL' (rise) or 'PUT' (fall). expiry_h in hours."""
        if not self.is_virtual:
            raise RuntimeError("Refusing: not virtual account")
        contract_type = "CALL" if direction.upper() in ("BUY", "CALL", "RISE") else "PUT"
        proposal = {
            "proposal": 1,
            "amount": stake,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": "USD",
            "duration": expiry_h,
            "duration_unit": "h",
            "symbol": symbol,
        }
        prop_resp = self.call(proposal)
        if "error" in prop_resp:
            raise RuntimeError(f"proposal error: {prop_resp['error']}")
        proposal_id = prop_resp["proposal"]["id"]
        ask_price = prop_resp["proposal"]["ask_price"]
        payout = prop_resp["proposal"]["payout"]
        log.info(f"Proposal {symbol} {contract_type} {expiry_h}h: ask={ask_price} payout={payout}")
        if dry_run:
            return {"dry_run": True, "ask_price": ask_price, "payout": payout, "contract_type": contract_type}
        buy_resp = self.call({"buy": proposal_id, "price": ask_price})
        if "error" in buy_resp:
            raise RuntimeError(f"buy error: {buy_resp['error']}")
        b = buy_resp["buy"]
        log.info(f"BOUGHT contract_id={b['contract_id']} buy_price={b['buy_price']}")
        return b

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ----- Aggregation helpers -----
def resample_ohlcv(df_1m: pd.DataFrame, freq: str) -> pd.DataFrame:
    return df_1m.resample(freq).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()


def get_15m_1h_4h(deriv: Deriv, deriv_symbol: str):
    """Fetch 1m candles from Deriv and aggregate. Returns (15m, 1h, 4h)."""
    df_15m = deriv.get_candles(deriv_symbol, 900, count=2000)   # ~21 days
    df_1h  = deriv.get_candles(deriv_symbol, 3600, count=2000)  # ~83 days
    df_4h  = deriv.get_candles(deriv_symbol, 14400, count=1000) # ~166 days
    return df_15m, df_1h, df_4h


# ----- Decision logic -----
def evaluate_pair(pair: str, df_15m, df_1h, df_4h, ctx) -> dict:
    """Returns {direction, score, v14_conf, old_conf, ok_to_trade, reason}."""
    rules = RULES[pair]
    # score_v14 expects datetime index UTC and OHLCV columns
    sc = score_v14(df_15m, df_1h, df_4h, PAIR_YF[pair], ctx).dropna()
    if len(sc) == 0:
        return {"ok_to_trade": False, "reason": "no_score"}
    last = sc.iloc[-1]
    last_t = sc.index[-1]
    score = float(last["score_v14"])
    vc = int(last["v14_confluence"])
    oc = int(last["confluence"])
    direction = "BUY" if score > 0 else "SELL"
    h_utc = last_t.hour
    lo, hi = rules["session_utc"]
    in_session = (lo <= h_utc <= hi)
    abs_score_ok = abs(score) >= rules["min_score"]
    vc_ok = vc >= rules["min_vc"]
    oc_ok = oc >= rules["min_oc"]
    ok = in_session and abs_score_ok and vc_ok and oc_ok
    reason = []
    if not in_session: reason.append(f"out_of_session h={h_utc} need {lo}-{hi}")
    if not abs_score_ok: reason.append(f"score {score:+.0f} need ±{rules['min_score']}")
    if not vc_ok: reason.append(f"v14conf {vc} need {rules['min_vc']}")
    if not oc_ok: reason.append(f"oldconf {oc} need {rules['min_oc']}")
    return {
        "ok_to_trade": ok, "reason": ";".join(reason) or "ALL OK",
        "direction": direction, "score": score, "v14_conf": vc, "old_conf": oc,
        "expiry_h": rules["expiry_h"], "ts": str(last_t),
    }


# ----- Trade log -----
def log_trade(row: dict):
    df_new = pd.DataFrame([row])
    if TRADES_CSV.exists():
        df_old = pd.read_csv(TRADES_CSV)
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_all = df_new
    df_all.to_csv(TRADES_CSV, index=False)


# ----- Main loop -----
def run_once(dry_run: bool = False):
    if not TOKEN:
        log.error("No DERIV_DEMO_TOKEN env var")
        return
    deriv = Deriv(TOKEN)
    try:
        deriv.connect()
        deriv.authorize()
        log.info(f"Loading cross-asset ctx (yfinance)...")
        ctx = download_context_assets("3mo")
        for pair, dsym in DERIV_SYMBOLS.items():
            try:
                d15, d1h, d4h = get_15m_1h_4h(deriv, dsym)
                eval_res = evaluate_pair(pair, d15, d1h, d4h, ctx)
                log.info(f"{pair}: {eval_res}")
                if eval_res["ok_to_trade"]:
                    direction = eval_res["direction"]
                    expiry_h = eval_res["expiry_h"]
                    contract_type = "CALL" if direction == "BUY" else "PUT"
                    log.info(f"-> SIGNAL {pair} {direction} {expiry_h}h score={eval_res['score']:+.0f}")
                    result = deriv.buy_contract(dsym, direction, expiry_h, STAKE_USD, dry_run=dry_run)
                    log_trade({
                        "ts_utc": datetime.now(timezone.utc).isoformat(),
                        "pair": pair, "deriv_symbol": dsym,
                        "direction": direction, "expiry_h": expiry_h,
                        "score": eval_res["score"], "v14_conf": eval_res["v14_conf"],
                        "old_conf": eval_res["old_conf"], "stake": STAKE_USD,
                        "contract_id": result.get("contract_id"),
                        "buy_price": result.get("buy_price"),
                        "payout": result.get("payout"),
                        "dry_run": dry_run,
                    })
            except Exception as e:
                log.exception(f"{pair} error: {e}")
    finally:
        deriv.close()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run_once(dry_run=dry_run)
