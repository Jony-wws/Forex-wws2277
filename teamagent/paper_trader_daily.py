"""paper_trader_daily — «Лучший прогноз дня» (запрос Jony 2026-05-01).

Третья параллельная стратегия. В отличие от:
  - paper_trader (free 70% gate на probability_pct, открывает много сделок)
  - paper_trader_stakan (≥8/11 голосов + 10-min reversal filter, focus на VP)

эта стратегия работает по-другому:

  - **Раз в сутки** (в фиксированное время UTC) сканирует ВСЕ 28 пар одновременно.
  - **Считает META-score** (объединяет ВСЕ источники сигналов системы):
        forecast.probability_pct (PROGNOZY-28)  ×1.0
        market_radar.overall_score              ×0.8
        stakan_signals.votes                    ×1.2
        reversal_filter.yes_count/5             ×1.0
        macro_tilt                              ×0.5
        cot_z                                   ×0.5
  - **Открывает 1 сделку на пару** (до 28 trades/день) — ТОЛЬКО если
    forecast.probability_pct ≥ 70% (HARD GATE по запросу Jony 2026-05-01).
    Если прогноз ниже 70% — пара пропускается сегодня (нет «лучших из плохих»).
  - **Адаптивный stake**: confidence ≥80% → $2 ; 70-80% → $1 ; 60-70% → $0.50 ; <60% → skip.
  - **Адаптивная экспирация**: 12-23 часа (до следующего цикла), от ATR.
  - **Auto-pause**: пара с rolling 20-trade WR < 60% → пауза 7 дней.

State-файлы отдельные (не пересекаются с другими стратегиями):
  - state/daily_open_trades.json
  - state/daily_closed_trades.json
  - state/daily_stats.json
  - state/daily_signals.json   — последний снапшот скана для UI
  - state/daily_paused_pairs.json

Запускается как child-процесс под orchestrator.
"""
from __future__ import annotations
import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import config, indicators as ind
from . import market_hours as mh

# market hours buffer — daily-trades могут идти долго (до 18ч), поэтому
# буфер чуть больше: 30 мин до закрытия рынка.
MARKET_CLOSE_BUFFER_MIN = 30
from .data import yahoo
try:
    from . import fundamentals as fund_mod  # для pair_macro_tilt(pair)
except Exception:  # pragma: no cover
    fund_mod = None
try:
    from . import cot as cot_mod  # для pair_cot_signal(pair)
except Exception:  # pragma: no cover
    cot_mod = None

log = logging.getLogger("daily")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "paper_trader_daily.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

# ────────── State files ──────────
FORECASTS_FILE = config.STATE_DIR / "forecasts.json"
RADAR_FILE = config.STATE_DIR / "market_radar.json"
STAKAN_SIGNALS_FILE = config.STATE_DIR / "stakan_signals.json"
FUNDAMENTALS_FILE = config.STATE_DIR / "agent_analyzer_fundamental_macro.json"
COT_FILE = config.STATE_DIR / "agent_analyzer_cot_positioning.json"

OPEN_FILE = config.STATE_DIR / "daily_open_trades.json"
CLOSED_FILE = config.STATE_DIR / "daily_closed_trades.json"
STATS_FILE = config.STATE_DIR / "daily_stats.json"
SIGNALS_FILE = config.STATE_DIR / "daily_signals.json"
PAUSED_FILE = config.STATE_DIR / "daily_paused_pairs.json"
LAST_RUN_FILE = config.STATE_DIR / "daily_last_run.json"
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_paper_trader_daily.json"

# ────────── Параметры стратегии ──────────
# Запуск раз в сутки в начало торгового дня по времени Jony (UTC+5 = Asia start).
# В UTC это 00:00 - 5 = 19:00 предыдущего дня. Делаем простую проверку
# по часу UTC: если час == DAILY_RUN_HOUR_UTC и это новый день → запуск.
DAILY_RUN_HOUR_UTC = 19            # 19:00 UTC = 00:00 UTC+5 = начало "торгового дня"
TICK_INTERVAL_SEC = 60             # каждую минуту проверяем «пора ли»

# Адаптивный stake (binary option payout 85%)
# Цель пользователя 2026-05-01: «минимум 1 прогноз на пару в день, итого 28/день».
# Поэтому стейкинг линейный — confidence просто масштабирует риск, но открываем
# почти всегда (отказ только если NEUTRAL или confidence < 5%).
STAKE_HIGH = 2.0                   # confidence ≥70% → двойной риск
STAKE_NORMAL = 1.0                 # 50-70%
STAKE_LOW = 0.5                    # 25-50%
STAKE_MICRO = 0.25                 # 5-25%  (минимальная экспозиция)
MIN_CONFIDENCE_FOR_TRADE = 5.0     # <5% confidence → действительно рынок мёртв, пропускаем

# Веса источников в META-score (нормализуем по сумме весов)
WEIGHTS = {
    "forecast_prob": 1.0,           # 50..92% → -100..+100 (по знаку score) после нормировки
    "radar_score": 0.8,             # уже [-100..+100]
    "stakan_votes": 1.2,            # 0..11 → -100..+100 (8/11 = +57)
    "reversal_filter": 1.0,         # 0..5 → -100..+100 (3/5 = +20)
    "macro_tilt": 0.5,              # -1..+1 (FRED) → -100..+100
    "cot_z": 0.5,                   # z-score → tanh-нормировка
}

# Адаптивная экспирация
MIN_EXPIRY_H = 12                  # минимум 12 часов
MAX_EXPIRY_H = 23                  # максимум 23 часа (до следующего цикла)

# Auto-pause
ROLLING_WINDOW = 20                # последние N сделок на паре
PAUSE_WR_THRESHOLD = 60.0          # rolling WR < 60% → пауза
PAUSE_DAYS = 7                     # на 7 дней
MIN_TRADES_FOR_PAUSE = 10          # не паузить пока < 10 сделок

# ────────── Утилиты ──────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        log.warning(f"corrupt {path.name}, resetting")
        return default


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))


def _heartbeat(tick: int) -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "paper_trader_daily",
        "category": "trader",
        "ts": _now().isoformat(),
        "pid": os.getpid(),
        "tick_count": tick,
    }))


# ────────── Meta-scoring ──────────

def _normalize_forecast(forecast: dict | None) -> tuple[float, str]:
    """probability_pct (50-92) + side → score [-100..+100].
    BUY 90% → +100 ; SELL 90% → -100 ; 50% → 0 (любая сторона).
    Returns (score, side).
    """
    if not forecast:
        return 0.0, "NEUTRAL"
    prob = float(forecast.get("probability_pct") or 50.0)
    side = (forecast.get("side") or "NEUTRAL").upper()
    # 50% → 0, 92% → 100; линейно
    magnitude = max(0.0, (prob - 50.0) / 42.0 * 100.0)
    if side == "BUY":
        return magnitude, "BUY"
    if side == "SELL":
        return -magnitude, "SELL"
    return 0.0, "NEUTRAL"


def _normalize_radar(radar_pair: dict | None) -> float:
    if not radar_pair:
        return 0.0
    return float(radar_pair.get("overall_score") or 0.0)


def _normalize_stakan_votes(stakan_signal: dict | None) -> tuple[float, str]:
    """Stakan votes 0..11 + direction → -100..+100."""
    if not stakan_signal:
        return 0.0, "NEUTRAL"
    votes = stakan_signal.get("votes") or {}
    yes = int(votes.get("yes") or 0)
    total = int(votes.get("total") or 11)
    direction = (stakan_signal.get("direction") or "NEUTRAL").upper()
    if total == 0:
        return 0.0, "NEUTRAL"
    # 0/11 → -100, 11/11 → +100, 5.5/11 → 0
    magnitude = (yes / total - 0.5) * 200.0
    if direction == "BUY":
        return max(0.0, magnitude), "BUY"
    if direction == "SELL":
        return -max(0.0, magnitude), "SELL"
    return 0.0, "NEUTRAL"


def _normalize_reversal(reversal: dict | None) -> float:
    """0..5 yes_count → 0..+100 (направление-нейтральное)."""
    if not reversal:
        return 0.0
    yes = int(reversal.get("yes_count") or 0)
    return min(100.0, yes / 5.0 * 100.0)


def _normalize_macro(fundamentals: dict | None, pair: str) -> float:
    """Use fundamentals.pair_macro_tilt(pair) — возвращает signed score [-100..+100]."""
    if fund_mod is None:
        return 0.0
    try:
        # `fundamentals` payload может приходить из FUNDAMENTALS_FILE — там лежит
        # `summary.currencies`. fund_mod.pair_macro_tilt принимает {currencies: ...}
        cur_block = None
        if fundamentals:
            cur_block = (fundamentals.get("summary") or {}).get("currencies") \
                        or fundamentals.get("currencies")
        tilt = fund_mod.pair_macro_tilt(pair, {"currencies": cur_block} if cur_block else None)
        score = float(tilt.get("tilt_score") or 0.0)
        side = (tilt.get("side") or "NEUTRAL").upper()
        if side == "SELL":
            score = -abs(score)
        elif side == "BUY":
            score = abs(score)
        return max(-100.0, min(100.0, score))
    except Exception as e:
        log.debug(f"_normalize_macro {pair}: {e}")
        return 0.0


def _normalize_cot(cot: dict | None, pair: str) -> float:
    """Use cot.pair_cot_signal(pair) — возвращает signed score через side+strength."""
    if cot_mod is None:
        return 0.0
    try:
        cur_block = None
        if cot:
            cur_block = (cot.get("summary") or {}).get("currencies") \
                        or cot.get("currencies")
        sig = cot_mod.pair_cot_signal(pair, {"currencies": cur_block} if cur_block else None)
        strength = float(sig.get("strength_pct") or 0.0)
        side = (sig.get("side") or "NEUTRAL").upper()
        if side == "BUY":
            return strength
        if side == "SELL":
            return -strength
        return 0.0
    except Exception as e:
        log.debug(f"_normalize_cot {pair}: {e}")
        return 0.0


def _meta_score(pair: str, snapshot: dict, radar: dict,
                stakan: dict, fundamentals: dict, cot: dict) -> dict:
    """Вычисляет META-score для пары.

    Returns:
      {
        "score": -100..+100 (signed magnitude),
        "side": BUY/SELL/NEUTRAL,
        "confidence_pct": 0..100 (abs of score),
        "components": {name: {value, weight}, ...}
      }
    """
    forecast = (snapshot.get("forecasts") or {}).get(pair)
    fcast_score, fcast_side = _normalize_forecast(forecast)

    radar_pair = (radar.get("pairs") or {}).get(pair)
    radar_score = _normalize_radar(radar_pair)

    stakan_pair = None
    for sig in (stakan.get("signals") or []):
        if sig.get("pair") == pair:
            stakan_pair = sig
            break
    stakan_score, stakan_side = _normalize_stakan_votes(stakan_pair)

    reversal_data = None
    if stakan_pair:
        reversal_data = stakan_pair.get("reversal_filter")
    reversal_magnitude = _normalize_reversal(reversal_data)

    macro_score = _normalize_macro(fundamentals, pair)
    cot_score = _normalize_cot(cot, pair)

    # Sum weighted signed magnitudes for direction
    components = {
        "forecast_prob": {"score": fcast_score, "weight": WEIGHTS["forecast_prob"],
                          "raw": forecast.get("probability_pct") if forecast else None,
                          "side": fcast_side},
        "radar_score": {"score": radar_score, "weight": WEIGHTS["radar_score"],
                        "raw": radar_pair.get("direction") if radar_pair else None},
        "stakan_votes": {"score": stakan_score, "weight": WEIGHTS["stakan_votes"],
                         "raw": (stakan_pair.get("votes") if stakan_pair else None),
                         "side": stakan_side},
        "reversal_filter": {"score": reversal_magnitude, "weight": WEIGHTS["reversal_filter"],
                            "raw": (reversal_data.get("yes_count") if reversal_data else None),
                            "note": "magnitude only — boosts confidence"},
        "macro_tilt": {"score": macro_score, "weight": WEIGHTS["macro_tilt"]},
        "cot_z": {"score": cot_score, "weight": WEIGHTS["cot_z"]},
    }
    weight_sum = sum(WEIGHTS.values())
    weighted = (
        WEIGHTS["forecast_prob"] * fcast_score
        + WEIGHTS["radar_score"] * radar_score
        + WEIGHTS["stakan_votes"] * stakan_score
        + WEIGHTS["macro_tilt"] * macro_score
        + WEIGHTS["cot_z"] * cot_score
    ) / weight_sum

    # reversal_filter boosts magnitude only (does not flip direction)
    boost = (WEIGHTS["reversal_filter"] * reversal_magnitude / weight_sum) * 0.3
    if weighted >= 0:
        weighted += boost
    else:
        weighted -= boost

    weighted = max(-100.0, min(100.0, weighted))
    # Чувствительный порог: 1.0, чтобы 28 пар чаще получали явное направление.
    # При истинной нейтральности (множество мелких разнонаправленных сигналов) —
    # confidence окажется <5 и сделка всё равно не откроется.
    if weighted > 1.0:
        side = "BUY"
    elif weighted < -1.0:
        side = "SELL"
    else:
        side = "NEUTRAL"

    return {
        "score": round(weighted, 2),
        "side": side,
        "confidence_pct": round(abs(weighted), 2),
        "components": components,
    }


# ────────── Auto-pause helpers ──────────

def _rolling_pair_wr(closed: list[dict], pair: str, window: int = ROLLING_WINDOW) -> tuple[float, int]:
    pair_trades = [t for t in closed if t.get("pair") == pair]
    pair_trades = pair_trades[-window:]
    if not pair_trades:
        return 100.0, 0
    wins = sum(1 for t in pair_trades if t.get("result") == "WIN")
    return (wins / len(pair_trades) * 100.0), len(pair_trades)


def _refresh_paused(paused: dict, closed: list[dict]) -> dict:
    """Применяет правило auto-pause: если pair имеет ≥MIN_TRADES_FOR_PAUSE сделок
    с rolling WR < PAUSE_WR_THRESHOLD → пауза до now+PAUSE_DAYS.
    Также снимает паузу если срок истёк.
    """
    now = _now()
    out = {}
    # удалить истёкшие
    for pair, info in (paused or {}).items():
        until = info.get("until")
        if until:
            until_dt = datetime.fromisoformat(until)
            if now < until_dt:
                out[pair] = info
    # добавить новые
    for pair in config.PAIRS:
        if pair in out:
            continue
        wr, n = _rolling_pair_wr(closed, pair)
        if n >= MIN_TRADES_FOR_PAUSE and wr < PAUSE_WR_THRESHOLD:
            out[pair] = {
                "since": now.isoformat(),
                "until": (now + timedelta(days=PAUSE_DAYS)).isoformat(),
                "rolling_wr": round(wr, 1),
                "trades_in_window": n,
                "reason": f"rolling_wr_{wr:.1f}_below_{PAUSE_WR_THRESHOLD}",
            }
            log.warning(f"PAUSE-DAILY {pair} for {PAUSE_DAYS}d (WR={wr:.1f}% over {n} trades)")
    return out


# ────────── Adaptive expiry ──────────

def _auto_expiry_hours(pair: str) -> int:
    """12-23 часа в зависимости от ATR_1h / 20d_median_ATR_1h."""
    bars_1h = yahoo.latest_bars(pair, "1h", 600)
    if bars_1h is None or bars_1h.empty:
        return 18  # дефолт
    atr_ser = ind.atr(bars_1h, 14).dropna()
    if atr_ser.empty:
        return 18
    atr_now = float(atr_ser.iloc[-1])
    atr_med = float(atr_ser.tail(480).median())  # последние 20 дней (24×20=480 1h bars)
    if atr_med <= 0:
        return 18
    ratio = atr_now / atr_med
    # высокая волатильность → короче (быстрее доходит); низкая → длиннее
    if ratio > 1.5:
        return MIN_EXPIRY_H
    if ratio < 0.7:
        return MAX_EXPIRY_H
    # линейная интерполяция между 0.7 и 1.5
    span = MAX_EXPIRY_H - MIN_EXPIRY_H
    h = MAX_EXPIRY_H - int((ratio - 0.7) / 0.8 * span)
    return max(MIN_EXPIRY_H, min(MAX_EXPIRY_H, h))


# ────────── Stake sizing ──────────

def _stake_for_confidence(confidence_pct: float) -> float:
    if confidence_pct >= 70.0:
        return STAKE_HIGH
    if confidence_pct >= 50.0:
        return STAKE_NORMAL
    if confidence_pct >= 25.0:
        return STAKE_LOW
    if confidence_pct >= MIN_CONFIDENCE_FOR_TRADE:
        return STAKE_MICRO
    return 0.0  # NEUTRAL → skip


# ────────── Settlement ──────────

def _settle_expired(open_trades: list[dict], closed_trades: list[dict]) -> int:
    settled = 0
    still_open = []
    for t in open_trades:
        if t["status"] != "open":
            continue
        expiry = datetime.fromisoformat(t["expiry_time"])
        if _now() < expiry:
            still_open.append(t)
            continue
        close_price = yahoo.settlement_price(t["pair"], expiry)
        if close_price is None:
            still_open.append(t)
            continue
        if t["side"] == "BUY":
            win = close_price > t["open_price"]
        else:
            win = close_price < t["open_price"]
        stake = float(t.get("stake_usd") or STAKE_NORMAL)
        payout = float(t.get("payout_pct") or config.PAYOUT_PCT)
        pnl = (stake * payout) if win else (-stake)
        result = "WIN" if win else "LOSS"
        t.update({
            "status": result,
            "close_price": close_price,
            "close_time": _now().isoformat(),
            "result": result,
            "pnl_usd": round(pnl, 2),
        })
        closed_trades.append(t)
        settled += 1
        log.info(f"CLOSE-DAILY {t['pair']} {t['side']} → {result} pnl={pnl:+.2f}")
    open_trades[:] = still_open
    return settled


# ────────── Sweep + open new daily picks ──────────

def _is_open_for_pair(open_trades: list[dict], pair: str) -> bool:
    return any(t.get("pair") == pair and t.get("status") == "open" for t in open_trades)


def _daily_sweep(open_trades: list[dict], paused: dict) -> tuple[int, list[dict]]:
    """Сканирует все 28 пар, для каждой считает META-score, открывает 1 сделку
    если confidence ≥ MIN_CONFIDENCE_FOR_TRADE и пара не на паузе.
    """
    snapshot = _load(FORECASTS_FILE, {"forecasts": {}})
    radar = _load(RADAR_FILE, {})
    stakan = _load(STAKAN_SIGNALS_FILE, {})
    fundamentals = _load(FUNDAMENTALS_FILE, {})
    cot = _load(COT_FILE, {})

    opened = 0
    signals: list[dict] = []
    now_ts = _now()

    # Market hours gate
    if not mh.is_market_open(now_ts):
        log.info("MARKET CLOSED — пропускаю открытие daily-сделок")
        return 0, signals
    safe_cap_h = mh.max_safe_expiry_hours(now_ts, MARKET_CLOSE_BUFFER_MIN)
    if safe_cap_h < 1:
        log.info(f"MARKET CLOSING — safe_cap={safe_cap_h}h, daily-trades пропуск")
        return 0, signals

    for pair in config.PAIRS:
        if pair in paused:
            until = paused[pair].get("until", "")
            signals.append({
                "pair": pair,
                "skip_reason": "paused",
                "paused_until": until,
                "rolling_wr": paused[pair].get("rolling_wr"),
            })
            continue

        if _is_open_for_pair(open_trades, pair):
            signals.append({"pair": pair, "skip_reason": "already_open"})
            continue

        try:
            meta = _meta_score(pair, snapshot, radar, stakan, fundamentals, cot)
        except Exception as e:
            log.exception(f"_meta_score failed {pair}: {e}")
            signals.append({"pair": pair, "skip_reason": f"error:{type(e).__name__}"})
            continue

        side = meta["side"]
        confidence = meta["confidence_pct"]
        signal_entry = {
            "pair": pair,
            "side": side,
            "confidence_pct": confidence,
            "meta_score": meta["score"],
            "components": meta["components"],
        }

        if side == "NEUTRAL" or confidence < MIN_CONFIDENCE_FOR_TRADE:
            signal_entry["skip_reason"] = f"confidence_{confidence:.1f}_below_{MIN_CONFIDENCE_FOR_TRADE}"
            signals.append(signal_entry)
            continue

        # HARD GATE 70%: forecast.probability_pct ОБЯЗАТЕЛЕН ≥ 70 (запрос Jony 2026-05-01).
        # Раньше daily-trader открывал сделки даже при WR<70%, но пользователь
        # явно потребовал «полностью 70% на ВСЕХ ячейках». Если прогноз ниже —
        # пропускаем эту пару сегодня (а не открываем «лучший из плохих»).
        f = (snapshot.get("forecasts") or {}).get(pair) or {}
        f_prob = float(f.get("probability_pct") or 0)
        if f_prob < 70.0:
            signal_entry["skip_reason"] = f"forecast_prob_{f_prob:.1f}%_below_70%"
            signal_entry["forecast_prob_pct"] = f_prob
            signals.append(signal_entry)
            continue

        stake = _stake_for_confidence(confidence)
        if stake <= 0:
            signal_entry["skip_reason"] = "stake_zero"
            signals.append(signal_entry)
            continue

        cp = yahoo.latest_price(pair)
        if cp is None:
            signal_entry["skip_reason"] = "no_price"
            signals.append(signal_entry)
            continue

        try:
            expiry_h = _auto_expiry_hours(pair)
        except Exception as e:
            log.warning(f"auto_expiry failed {pair}: {e}")
            expiry_h = 18

        # Clip до закрытия рынка
        expiry_h = mh.clip_expiry_hours(expiry_h, now_ts,
                                          MARKET_CLOSE_BUFFER_MIN)
        if expiry_h <= 0:
            signal_entry["skip_reason"] = "market_closing"
            signals.append(signal_entry)
            continue

        expiry_time = now_ts + timedelta(hours=expiry_h)
        trade = {
            "id": "daily-" + str(uuid.uuid4())[:10],
            "strategy": "daily",
            "pair": pair,
            "side": side,
            "open_price": cp,
            "open_time": now_ts.isoformat(),
            "expiry_time": expiry_time.isoformat(),
            "expiry_hours": expiry_h,
            "stake_usd": stake,
            "payout_pct": float(config.PAYOUT_PCT),
            "confidence_pct": confidence,
            "meta_score": meta["score"],
            "meta_components_at_open": meta["components"],
            "status": "open",
        }
        open_trades.append(trade)
        opened += 1
        signal_entry["opened"] = True
        signal_entry["expiry_hours"] = expiry_h
        signal_entry["stake_usd"] = stake
        signals.append(signal_entry)
        log.info(
            f"OPEN-DAILY {pair} {side} @ {cp} confidence={confidence:.1f}% "
            f"stake=${stake} expiry={expiry_h}h meta={meta['score']:+.1f}"
        )

    return opened, signals


# ────────── Stats ──────────

def _compute_stats(closed: list[dict]) -> dict:
    if not closed:
        return {"strategy": "daily", "total": 0, "wins": 0, "losses": 0,
                "win_rate_pct": 0.0, "total_pnl_usd": 0.0}
    wins = sum(1 for t in closed if t.get("result") == "WIN")
    total = len(closed)
    pnl = sum(float(t.get("pnl_usd") or 0) for t in closed)
    # rolling 30 trades
    last30 = closed[-30:]
    last30_wins = sum(1 for t in last30 if t.get("result") == "WIN")
    return {
        "strategy": "daily",
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate_pct": round(wins / total * 100.0, 1),
        "total_pnl_usd": round(pnl, 2),
        "rolling_30_win_rate_pct": round(last30_wins / len(last30) * 100.0, 1) if last30 else 0.0,
        "rolling_30_total": len(last30),
    }


# ────────── Main loop ──────────

def _should_run_now(last_run: dict) -> bool:
    """Запускаем daily sweep если:
    - нет last_run (первый запуск), или
    - текущий час UTC == DAILY_RUN_HOUR_UTC и last_run был не сегодня.
    """
    now = _now()
    last_iso = last_run.get("ts")
    if not last_iso:
        return True
    last = datetime.fromisoformat(last_iso)
    if now.hour == DAILY_RUN_HOUR_UTC and now.date() != last.date():
        return True
    return False


def cycle_once(force: bool = False) -> dict:
    """Один тик. Закрывает истёкшие, при необходимости запускает дневной sweep."""
    open_trades = _load(OPEN_FILE, [])
    closed = _load(CLOSED_FILE, [])
    paused = _load(PAUSED_FILE, {})
    last_run = _load(LAST_RUN_FILE, {})

    settled = _settle_expired(open_trades, closed)

    paused = _refresh_paused(paused, closed)

    halt = config.STATE_DIR / "TRADING_HALTED.flag"
    swept = False
    if halt.exists():
        log.info("paper_trader_daily: TRADING_HALTED — пропускаю sweep")
        opened, signals = 0, []
    elif force or _should_run_now(last_run):
        opened, signals = _daily_sweep(open_trades, paused)
        last_run = {"ts": _now().isoformat(), "opened": opened,
                    "scanned_pairs": len(config.PAIRS)}
        _save(LAST_RUN_FILE, last_run)
        swept = True
    else:
        opened, signals = 0, []

    _save(OPEN_FILE, open_trades)
    _save(CLOSED_FILE, closed)
    _save(PAUSED_FILE, paused)
    # Only OVERWRITE signals snapshot when we actually swept;
    # otherwise keep last day's scan visible to user (don't blank the UI).
    if swept:
        _save(SIGNALS_FILE, {
            "as_of": _now().isoformat(),
            "min_confidence": MIN_CONFIDENCE_FOR_TRADE,
            "weights": WEIGHTS,
            "next_run_hour_utc": DAILY_RUN_HOUR_UTC,
            "signals": signals,
        })
    stats = _compute_stats(closed)
    _save(STATS_FILE, stats)

    return {"opened": opened, "settled": settled, "paused_count": len(paused),
            "stats": stats}


def main() -> int:
    log.info("paper_trader_daily started (interval=%ds, daily_run_hour_utc=%d)",
             TICK_INTERVAL_SEC, DAILY_RUN_HOUR_UTC)

    stop = False

    def _stop(signum, frame):
        nonlocal stop
        stop = True
        log.info(f"paper_trader_daily: signal {signum}, stopping")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    tick = 0
    while not stop:
        try:
            cycle_once()
        except Exception as e:
            log.exception(f"cycle_once: {e}")
        tick += 1
        _heartbeat(tick)
        for _ in range(TICK_INTERVAL_SEC):
            if stop:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
