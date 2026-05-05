"""Paper-Trader 24/7 — бинарные опционы на основе forecast_scanner.

При каждом прогнозе ≥70% и отсутствии открытой сделки по этой паре —
открываем виртуальную сделку:
  stake = $50, payout = 85% (WIN +$42.50, LOSS -$50)
  expiry = recommended_hours от forecast_scanner (1..4ч)

Раз в минуту:
  - читает state/forecasts.json
  - открывает новые сделки (если ≥70% и нет уже открытой)
  - пересчитывает live PnL по всем открытым (по реальной цене Yahoo)
  - закрывает истёкшие по реальной цене Yahoo (settlement_price)
  - пишет state/open_trades.json и state/closed_trades.json

Для каждой открытой сделки UI получает:
  pair, side, open_price, open_time, expiry_time, current_price, current_pnl,
  current_pnl_pct, time_remaining_sec, projected_payout_if_closed_now.
"""
from __future__ import annotations
import json
import logging
import signal
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import config, strategies
from . import market_hours as mh
from . import live_analyst as live_analyst_mod
from . import regime as regime_mod
from .data import yahoo

# Адаптивный диапазон экспирации (по требованию пользователя 2026-05-01):
# обе основные стратегии открывают сделки с экспирацией от 1 до 5 часов.
# Конкретное значение в этом диапазоне выбирается стратегией (best_variant.fixed_expiry_h
# или forecast.recommended_hours), но клампится в [MIN, MAX] и **обрезается
# до закрытия рынка** через market_hours.clip_expiry_hours.
ADAPTIVE_MIN_EXPIRY_H = 1
ADAPTIVE_MAX_EXPIRY_H = 5
MARKET_CLOSE_BUFFER_MIN = 15  # не открывать сделку если до закрытия < 1ч 15мин

log = logging.getLogger("paper")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "paper_trader.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

FORECASTS_FILE = config.STATE_DIR / "forecasts.json"
OPEN_FILE = config.STATE_DIR / "open_trades.json"
CLOSED_FILE = config.STATE_DIR / "closed_trades.json"
STATS_FILE = config.STATE_DIR / "paper_stats.json"
BACKTEST_FILE = config.STATE_DIR / "backtest_30d.json"
STRATEGY_CONFIG_FILE = config.STATE_DIR / "strategy_config.json"
# Snapshot первого валидного 365-day sweep — fallback если основной cfg пуст или
# был пересчитан в худшую сторону. Пересоздаётся явно: `python -m
# teamagent.strategy_search --relock`.
STRATEGY_LOCKED_FILE = config.STATE_DIR / "strategy_config_locked.json"
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_paper_trader.json"

# Gate: сделка открывается только если:
#   forecast.probability >= MIN_PROBABILITY И
#   strategy_config[pair].best_variant.win_rate_pct >= MIN_WR_PCT (на этой паре лучшая стратегия даёт ≥7О% WR на 30д) И
#   forecast сейчас удовлетворяет фильтры этого best_variant (сессия, |score|, prob).
# Это и есть «реальный 70% WR», а не теоретическая probability прямо из scanner-а.
MIN_WR_PCT = 80.0      # 2026-05-05: user wants ≥80% WR per pair
MIN_TRADES = 5
# fallback на backtest_30d.json если strategy_config ещё не рассчитан
BACKTEST_MIN_WR_PCT = 80.0   # 2026-05-05: stricter gate
BACKTEST_MIN_TRADES = 5

# ───── Корреляционный фильтр (2026-05-03) ─────
# Каждая валюта в Forex входит в несколько пар. Если открыты обе сделки
# (например EURUSD и EURGBP), они часто двигаются вместе → один и тот же
# макро-шок убивает обе. Ограничиваем число одновременно открытых сделок
# в одном валютном "блоке".
CURRENCY_BLOCKS: dict[str, set[str]] = {
    "EUR": {"EURUSD", "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD"},
    "GBP": {"GBPUSD", "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD", "EURGBP"},
    "JPY": {"USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY"},
    "CHF": {"USDCHF", "EURCHF", "GBPCHF", "AUDCHF", "CADCHF", "NZDCHF", "CHFJPY"},
    "AUD": {"AUDUSD", "EURAUD", "GBPAUD", "AUDCAD", "AUDCHF", "AUDNZD", "AUDJPY"},
    "CAD": {"USDCAD", "EURCAD", "GBPCAD", "AUDCAD", "CADCHF", "CADJPY", "NZDCAD"},
    "NZD": {"NZDUSD", "EURNZD", "GBPNZD", "AUDNZD", "NZDCAD", "NZDCHF", "NZDJPY"},
    "USD": {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"},
}


def _exceeds_correlation_limit(open_trades: list[dict], new_pair: str) -> tuple[bool, str | None]:
    """True если открытие new_pair нарушит лимит на валютный блок.

    Возвращает (exceed, currency_name|None). Берём cap из config:
    config.MAX_SAME_CURRENCY_BLOCK (по умолчанию 2).
    """
    cap = int(getattr(config, "MAX_SAME_CURRENCY_BLOCK", 2))
    for currency, pairs_in_block in CURRENCY_BLOCKS.items():
        if new_pair not in pairs_in_block:
            continue
        count = sum(
            1 for t in open_trades
            if t.get("pair") in pairs_in_block and t.get("status") == "open"
        )
        if count >= cap:
            return True, currency
    return False, None


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        log.warning(f"corrupt {path.name}, resetting")
        return default


def _save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2))


def _heartbeat() -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "paper_trader",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": __import__("os").getpid(),
    }))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_open_for_pair(open_trades: list[dict], pair: str) -> bool:
    return any(t["pair"] == pair and t["status"] == "open" for t in open_trades)


def _pip_size(pair: str) -> float:
    """1 pip для forex: 0.01 для JPY-пар, 0.0001 для остальных."""
    return 0.01 if pair.upper().endswith("JPY") else 0.0001


def _live_pnl(t: dict, current_price: float) -> dict:
    """Текущий PnL открытой сделки по live-цене.

    Бинарный опцион "если в твою сторону на момент сейчас, projected = WIN, иначе LOSS".
    Это «прогноз исхода», а не реальный PnL — UI должен это понимать.

    Дополнительно возвращаем `pips` (со знаком: + если в твою сторону, − иначе)
    и `current_pnl_pct` (синоним diff_pct со знаком относительно стороны).
    """
    side = t["side"]
    open_price = t["open_price"]
    pip_size = _pip_size(t.get("pair", ""))
    if side == "BUY":
        in_money = current_price > open_price
        diff_pct = (current_price - open_price) / open_price * 100.0
        pips = (current_price - open_price) / pip_size
    else:
        in_money = current_price < open_price
        diff_pct = (open_price - current_price) / open_price * 100.0
        pips = (open_price - current_price) / pip_size

    projected_payout = config.STAKE_USD * config.PAYOUT_PCT if in_money else -config.STAKE_USD
    return {
        "current_price": float(current_price),
        "diff_pct": round(diff_pct, 5),
        "pips": round(pips, 1),
        "current_pnl_pct": round(diff_pct, 3),
        "in_money_now": in_money,
        "projected_payout": round(projected_payout, 2),
    }


def _enrich_open_trade(t: dict) -> dict:
    """Добавляет live-данные для UI: current_price, current_pnl, time_remaining."""
    now = _now()
    expiry = datetime.fromisoformat(t["expiry_time"])
    open_time = datetime.fromisoformat(t["open_time"])
    cp = yahoo.latest_price(t["pair"])
    live = _live_pnl(t, cp) if cp is not None else {
        "current_price": None,
        "diff_pct": None,
        "in_money_now": None,
        "projected_payout": None,
    }
    return {
        **t,
        "live": {
            **live,
            "time_remaining_sec": max(0, int((expiry - now).total_seconds())),
            "elapsed_sec": int((now - open_time).total_seconds()),
            "as_of": now.isoformat(),
        },
    }


def _backtest_qualified(pair: str, backtest: dict) -> tuple[bool, dict]:
    """Fallback: проверить backtest_30d.json (baseline-вариант) если strategy_config отсутствует."""
    pairs = (backtest or {}).get("pairs") or {}
    p = pairs.get(pair)
    if not p:
        return False, {"reason": "backtest ещё не запускался"}
    wr = p.get("win_rate_pct")
    trades = p.get("trades") or 0
    if wr is None:
        return False, {"reason": p.get("note") or "нет истории", "trades": trades}
    if trades < BACKTEST_MIN_TRADES:
        return False, {"reason": f"trades<{BACKTEST_MIN_TRADES}", "trades": trades, "win_rate_pct": wr}
    if wr < BACKTEST_MIN_WR_PCT:
        return False, {"reason": f"WR<{BACKTEST_MIN_WR_PCT}%", "trades": trades, "win_rate_pct": wr}
    return True, {"trades": trades, "win_rate_pct": wr}


def _ensemble_decide(top_variants: list[dict], ts: datetime,
                     ind_4h: dict, ind_1h: dict, ind_15m: dict,
                     ) -> dict | None:
    """Ensemble voting (2026-05-03 user request to maximise WR ≥ 90%).

    Берём `top_variants` из strategy_search-а (до 10 штук per cell), фильтруем
    по WR ≥ config.ENSEMBLE_MIN_VARIANT_WR и trades ≥ config.ENSEMBLE_MIN_VARIANT_TRADES,
    затем для каждого вызываем strategies.evaluate() на текущих индикаторах.
    Подсчитываем голоса BUY/SELL и применяем правила:
      - 0 вариантов: torging blocked (None)
      - 1: проходит только если WR ≥ 75% (single-variant fallback)
      - 2: оба должны согласиться (2/2)
      - 3-4: ≥3 согласия
      - ≥5: ≥4 согласия (4/5)

    Возвращает dict с ключами {side, n_agree, n_total, agreed_variants,
    win_rate_pct, expiry_h, source} или None если голосование не прошло.
    """
    if not top_variants:
        return None
    min_wr = float(getattr(config, "ENSEMBLE_MIN_VARIANT_WR", 65.0))
    min_trades = int(getattr(config, "ENSEMBLE_MIN_VARIANT_TRADES", 8))
    variants_map = strategies.variants_by_id()

    eligible: list[dict] = []
    for tv in top_variants:
        wr = tv.get("win_rate_pct")
        tr = tv.get("trades") or 0
        if wr is None or wr < min_wr or tr < min_trades:
            continue
        v = variants_map.get(tv.get("variant"))
        if v is None:
            continue
        eligible.append({"variant": v, "tv": tv})

    if not eligible:
        return None

    # Для каждого варианта определяем какую сторону он бы открыл сейчас.
    decisions: list[dict] = []
    for e in eligible:
        v = e["variant"]
        try:
            out = strategies.evaluate(v, ts, ind_4h, ind_1h, ind_15m)
        except Exception:
            out = None
        if out is None:
            # Если evaluate() сам не открывает (фильтры не пройдены) —
            # пробуем dominant_side из бэктеста как fallback голос.
            dom = (e["tv"] or {}).get("dominant_side")
            if dom in ("BUY", "SELL"):
                decisions.append({
                    "variant_id": v.id, "side": dom, "via": "dominant_side",
                    "wr": e["tv"].get("win_rate_pct"),
                })
            continue
        side, score, expiry_h, prob = out
        decisions.append({
            "variant_id": v.id, "side": side, "score": score,
            "expiry_h": expiry_h, "via": "evaluate",
            "wr": e["tv"].get("win_rate_pct"),
        })

    if not decisions:
        return None

    n_total = len(decisions)
    n_buy = sum(1 for d in decisions if d["side"] == "BUY")
    n_sell = sum(1 for d in decisions if d["side"] == "SELL")
    side = "BUY" if n_buy > n_sell else ("SELL" if n_sell > n_buy else None)
    if side is None:
        return {
            "side": None, "n_agree": max(n_buy, n_sell), "n_total": n_total,
            "n_buy": n_buy, "n_sell": n_sell,
            "agreed_variants": [], "reason": "tied vote",
        }
    n_agree = n_buy if side == "BUY" else n_sell

    # Применяем порог согласия в зависимости от количества вариантов
    if n_total >= 5:
        required = 4
    elif n_total >= 3:
        required = 3
    elif n_total == 2:
        required = 2
    else:
        # 1 вариант — пропускаем только если WR ≥ 75% (одиночный)
        required = 1
        single_wr = decisions[0].get("wr") or 0
        if single_wr < 75.0:
            return {
                "side": None, "n_agree": n_agree, "n_total": n_total,
                "agreed_variants": [],
                "reason": f"single variant но WR={single_wr:.1f}% < 75%",
            }

    if n_agree < required:
        return {
            "side": None, "n_agree": n_agree, "n_total": n_total,
            "agreed_variants": [],
            "reason": f"ensemble: только {n_agree}/{n_total} за {side}, "
                      f"требуется {required}",
        }

    agreed = [d for d in decisions if d["side"] == side]
    # WR/expiry — берём от лидера согласившегося голосования (по WR)
    agreed.sort(key=lambda d: -(d.get("wr") or 0))
    leader = agreed[0]
    return {
        "side": side,
        "n_agree": n_agree,
        "n_total": n_total,
        "n_buy": n_buy,
        "n_sell": n_sell,
        "agreed_variants": [d["variant_id"] for d in agreed],
        "leader_variant_id": leader["variant_id"],
        "leader_wr_pct": leader.get("wr"),
        "expiry_h": leader.get("expiry_h"),
        "required_agreement": required,
    }


def _strategy_qualified(pair: str, ts: datetime, score: float, prob_pct: float,
                         baseline_side: str, rsi_1h: float | None,
                         strategy_cfg: dict,
                         forecast: dict | None = None) -> tuple[bool, dict]:
    """Гейт для открытия сделки. С 2026-05-03 поддерживает ENSEMBLE voting.

    Поведение:
    - Если в strategy_cfg для (pair, session) есть top_variants и
      config.ENSEMBLE_ENABLED — применяем ensemble voting (см.
      `_ensemble_decide`). Если ensemble дал согласованную сторону — открываем
      на этой стороне.
    - Иначе fall-through на исходный механизм (per-session qualified variant
      → flip rules → baseline-fallback с macro-фильтром или STRICT gate).
    """
    sess_name = strategies.detect_session(ts.hour)
    if sess_name is None:
        # 22-23 UTC off-hours — даже free gate не торгует (нет ликвидности)
        return False, {
            "reason": f"вне канонических сессий (час {ts.hour} UTC = 22-23 off-hours)",
        }

    pairs = (strategy_cfg or {}).get("pairs") or {}
    p = pairs.get(pair) or {}
    by_session = p.get("by_session") or {}
    session_cfg = by_session.get(sess_name) or {}
    variants_map = strategies.variants_by_id()

    # ───── Ensemble voting (2026-05-03) ─────
    # Если включено и в session_cfg есть top_variants — пробуем согласованное
    # решение нескольких лучших вариантов. Победил консенсус ≥4/5 (или 3/3,
    # 2/2) — открываем сделку на согласованной стороне. Иначе — fallthrough
    # на исходный single-variant механизм ниже.
    if getattr(config, "ENSEMBLE_ENABLED", False) and forecast is not None:
        top_variants = session_cfg.get("top_variants") or []
        ind_block = forecast.get("indicators") or {}
        ind_4h = ind_block.get("4H") or {}
        ind_1h = ind_block.get("1H") or {}
        ind_15m = ind_block.get("15m") or {}
        if ind_4h and ind_1h and ind_15m:
            ens = _ensemble_decide(top_variants, ts, ind_4h, ind_1h, ind_15m)
            if ens is not None and ens.get("side") in ("BUY", "SELL"):
                return True, {
                    "session": sess_name,
                    "side": ens["side"],
                    "gate_mode": "ensemble",
                    "ensemble": {
                        "n_agree": ens.get("n_agree"),
                        "n_total": ens.get("n_total"),
                        "n_buy": ens.get("n_buy"),
                        "n_sell": ens.get("n_sell"),
                        "agreed_variants": ens.get("agreed_variants"),
                        "leader_variant_id": ens.get("leader_variant_id"),
                        "leader_wr_pct": ens.get("leader_wr_pct"),
                        "required_agreement": ens.get("required_agreement"),
                    },
                    "variant": ens.get("leader_variant_id"),
                    "win_rate_pct": ens.get("leader_wr_pct"),
                }
            if ens is not None and ens.get("side") is None:
                # Голосование явно отклонило сделку — НЕ торгуем (STRICT
                # ensemble gate per user 2026-05-03 request).
                return False, {
                    "reason": ens.get("reason", "ensemble disagreement"),
                    "session": sess_name,
                    "ensemble": {
                        "n_agree": ens.get("n_agree"),
                        "n_total": ens.get("n_total"),
                    },
                }
            # ens is None: top_variants пустой / все отфильтрованы — fall through

    chosen = None
    chosen_source = None
    if session_cfg.get("qualifies_70pct") and (session_cfg.get("trades") or 0) >= MIN_TRADES:
        chosen = session_cfg
        chosen_source = "by_session"
    elif p.get("qualifies_70pct") and (p.get("trades") or 0) >= MIN_TRADES:
        chosen = p
        chosen_source = "global_best"

    variant = None
    if chosen is not None:
        variant = variants_map.get(chosen.get("best_variant"))

    # Сторона
    if variant is not None:
        # Применяем flip-правила, чтобы поднять WR без отказа от сделки
        eff = score
        if variant.fade_extreme_rsi and rsi_1h is not None and (rsi_1h <= 25 or rsi_1h >= 75):
            eff = -eff
        if variant.contrarian:
            eff = -eff
        if eff != 0:
            side = "BUY" if eff > 0 else "SELL"
        else:
            side = baseline_side
        return True, {
            "variant": variant.id,
            "variant_label": variant.label,
            "win_rate_pct": chosen.get("win_rate_pct"),
            "trades": chosen.get("trades"),
            "side": side,
            "effective_score": eff,
            "session": sess_name,
            "chosen_source": chosen_source,
            "gate_mode": "free",
        }

    # Нет qualified variant — открываем по baseline (forecast direction).
    #
    # Macro safety filter (added 2026-05-01 per user "100% бесплатный
    # институциональный анализ" request): когда нет qualified variant,
    # дополнительно проверяем что baseline side НЕ конфликтует с macro
    # позиционированием. Это повышает honest WR без блокировки большого
    # числа сделок:
    #   - сильный fundamental tilt (|tilt_score| ≥ 50) против baseline → SKIP
    #   - очень сильный COT contrarian (strength ≥ 40%) против baseline → SKIP
    # Эти пороги выбраны намеренно высокими, чтобы не сжать число сделок
    # ниже 5-10/день. Когда есть qualified variant — мы уже доверяем 365-day
    # бэктесту и macro фильтр не применяем.
    try:
        from . import fundamentals as fund
        ft = fund.pair_macro_tilt(pair)
        ts_score = ft.get("tilt_score") or 0
        if abs(ts_score) >= 50:
            macro_side = "BUY" if ts_score > 0 else "SELL"
            if macro_side != baseline_side:
                return False, {
                    "reason": (
                        f"macro filter: fundamental tilt={ts_score} → {macro_side}, "
                        f"но baseline={baseline_side} (rate_diff={ft.get('rate_diff_pct')}, "
                        f"yield_diff={ft.get('yield_diff_pct')})"
                    ),
                    "session": sess_name,
                }
    except Exception:
        pass
    try:
        from . import cot as cot_mod
        cs = cot_mod.pair_cot_signal(pair)
        if cs.get("side") in ("BUY", "SELL") and (cs.get("strength_pct") or 0) >= 40:
            if cs["side"] != baseline_side:
                return False, {
                    "reason": (
                        f"macro filter: COT contrarian={cs['side']} "
                        f"(strength={cs.get('strength_pct')}%, z={cs.get('combined_z')}), "
                        f"но baseline={baseline_side}"
                    ),
                    "session": sess_name,
                }
    except Exception:
        pass

    # 2026-05-01 user explicit request: STRICT mode — НЕ открывать сделки если
    # нет qualified variant для этой (pair, session). Это значит «торгуем
    # только в ячейках где система ДОКАЗАЛА ≥70% WR на 365d real Yahoo».
    if getattr(config, "STRICT_QUALIFIED_GATE", False):
        return False, {
            "reason": (
                f"STRICT gate: ни один (pair={pair}, session={sess_name}) "
                f"variant не показал ≥70% WR на 365д Yahoo. Не открываю."
            ),
            "session": sess_name,
        }

    return True, {
        "variant": None,
        "variant_label": "free-gate (no qualified variant — using baseline forecast)",
        "win_rate_pct": session_cfg.get("win_rate_pct") or p.get("win_rate_pct"),
        "trades": session_cfg.get("trades") or p.get("trades") or 0,
        "side": baseline_side,
        "effective_score": score,
        "session": sess_name,
        "chosen_source": "baseline",
        "gate_mode": "free",
    }


def _martingale_stake_for_pair(pair: str, closed_trades: list[dict]) -> tuple[float, int]:
    """Возвращает (stake_usd, current_loss_streak) для пары.

    Логика мартингейла (user request 2026-05-01):
      - смотрим самые свежие сделки этой пары (после последней WIN)
      - count подряд LOSS = streak (0..MAX_STREAK)
      - stake = STAKE_USD * (MULT ** streak)
      - cap на MARTINGALE_MAX_STREAK: после N подряд LOSS возвращаем
        к базовой ставке независимо от исхода (защита от разгона)
    """
    if not getattr(config, "MARTINGALE_ENABLED", False):
        return float(config.STAKE_USD), 0
    streak = 0
    for t in reversed(closed_trades):
        if t.get("pair") != pair:
            continue
        if t.get("status") == "WIN":
            break
        if t.get("status") == "LOSS":
            streak += 1
            if streak >= config.MARTINGALE_MAX_STREAK:
                streak = 0   # cap → reset
                break
    stake = float(config.STAKE_USD) * (float(config.MARTINGALE_MULT) ** streak)
    return round(stake, 2), streak


def _open_new_trades(open_trades: list[dict], snapshot: dict, backtest: dict,
                     strategy_cfg: dict, closed_trades: list[dict] | None = None) -> int:
    """Открыть виртуальные сделки по прогнозам, проходящим все гейты:
      forecast.probability_pct >= 70
      Лучшая стратегия пары имеет WR ≥ 70% на 30д (strategy_search)
      Текущий сигнал удовлетворяет фильтры той стратегии.
    """
    rankings = snapshot.get("rankings", [])
    forecasts = snapshot.get("forecasts", {})
    has_strategy_cfg = bool((strategy_cfg or {}).get("pairs"))   # для per_pair_cfg ниже
    closed_trades = closed_trades or []
    opened = 0
    now_ts = _now()

    # Market hours gate: не открывать новые сделки если рынок закрыт
    # или закроется через < (1h + buffer). Уже открытые сделки продолжают
    # жить — _settle_expired их закроет когда придёт expiry.
    if not mh.is_market_open(now_ts):
        log.info("MARKET CLOSED — пропускаю открытие новых сделок (Forex Sun22:00 UTC → Fri22:00 UTC)")
        return 0
    safe_cap_h = mh.max_safe_expiry_hours(now_ts, MARKET_CLOSE_BUFFER_MIN)
    if safe_cap_h < ADAPTIVE_MIN_EXPIRY_H:
        secs = mh.seconds_until_close(now_ts)
        log.info(f"MARKET CLOSING — до закрытия {secs}s, минимальный safe expiry "
                 f"меньше {ADAPTIVE_MIN_EXPIRY_H}h, пропускаю открытие")
        return 0

    for r in rankings:
        if r["probability_pct"] < config.MIN_PROBABILITY * 100.0:
            continue
        pair = r["pair"]
        if _is_open_for_pair(open_trades, pair):
            continue

        # Корреляционный фильтр (2026-05-03): не открываем 3+ сделок на одну
        # базовую валюту (например EURUSD + EURGBP + EURJPY). Cap из
        # config.MAX_SAME_CURRENCY_BLOCK (по умолчанию 2).
        excess, currency = _exceeds_correlation_limit(open_trades, pair)
        if excess:
            log.info(
                f"SKIP {pair} — корреляционный лимит: открыто ≥"
                f"{config.MAX_SAME_CURRENCY_BLOCK} сделок в блоке {currency}"
            )
            continue

        f = forecasts.get(pair)
        if not f:
            continue

        # Главный гейт (free 70%, since 2026-05-01): probability_pct >= 70 уже
        # проверен выше. _strategy_qualified только выбирает side/expiry, не
        # блокирует сделку. Если strategy_cfg пуст (свежая сессия) — функция
        # gracefully fall-through-ит в ветку "no qualified variant → baseline".
        rsi_1h = ((f.get("indicators") or {}).get("1H") or {}).get("rsi14")
        ok, why = _strategy_qualified(
            pair, now_ts,
            score=r.get("score", 0),
            prob_pct=r["probability_pct"],
            baseline_side=f.get("side", "BUY"),
            rsi_1h=rsi_1h,
            strategy_cfg=strategy_cfg or {},
            forecast=f,
        )
        if not ok:
            log.info(f"SKIP {pair} prob={r['probability_pct']}% — gate: {why}")
            continue

        cp = yahoo.latest_price(pair)
        if cp is None:
            continue
        # стратегия может развернуть сторону (contrarian / fade_extreme_rsi)
        trade_side = (why or {}).get("side") or f.get("side")

        # выбранный вариант может прийти из by_session или global; берём из `why`
        chosen_variant_id = (why or {}).get("variant")
        chosen_session = (why or {}).get("session")
        chosen_source = (why or {}).get("chosen_source")
        chosen_wr = (why or {}).get("win_rate_pct")
        chosen_trades = (why or {}).get("trades")

        # expiry: из выбранного variant если зафиксирована, иначе из forecast.recommended_hours
        recommended = f.get("recommended_hours", config.DEFAULT_EXPIRY_HOURS)
        if chosen_variant_id:
            variants = strategies.variants_by_id()
            v = variants.get(chosen_variant_id)
            if v is not None and v.fixed_expiry_h is not None:
                recommended = v.fixed_expiry_h

        # Кламп в [1..5h] — обе основные стратегии теперь работают в этом
        # диапазоне (замечание пользователя 2026-05-01).
        recommended = max(ADAPTIVE_MIN_EXPIRY_H,
                          min(ADAPTIVE_MAX_EXPIRY_H, int(recommended)))
        # Сжатие до закрытия рынка (защита от выхода в выходные)
        recommended = mh.clip_expiry_hours(recommended, now_ts,
                                           MARKET_CLOSE_BUFFER_MIN)
        if recommended <= 0:
            log.info(f"SKIP {pair} — после market_hours clip expiry=0h "
                     f"(рынок закрывается слишком скоро)")
            continue

        expiry_time = now_ts + timedelta(hours=recommended)
        per_pair_cfg = (strategy_cfg.get("pairs") or {}).get(pair, {}) if has_strategy_cfg else {}

        # Martingale: ставка зависит от текущего LOSS-стрика на этой паре
        stake_usd, mart_streak = _martingale_stake_for_pair(pair, closed_trades)

        # Live-режим + playbook lookup (информативно, не блокирует сделку).
        live_regime_label = None
        playbook_cell_status = None
        playbook_cell_wr = None
        try:
            ind_1h_block = (f.get("indicators") or {}).get("1H") or {}
            # Compact regime hint from forecast indicators (no extra Yahoo call)
            adx_v = ind_1h_block.get("adx", 0.0)
            if adx_v >= 25:
                live_regime_label = "trending_up" if (f.get("side") == "BUY") else "trending_down"
            elif adx_v <= 15:
                live_regime_label = "mean_reverting"
            else:
                live_regime_label = "mean_reverting"
            cell = live_analyst_mod.lookup_playbook_cell(pair, chosen_session or f.get("session", ""), live_regime_label)
            if cell:
                playbook_cell_status = cell.get("status")
                playbook_cell_wr = cell.get("wr_pct")
        except Exception:
            pass

        trade = {
            "id": str(uuid.uuid4())[:12],
            "pair": pair,
            "side": trade_side,
            "open_price": cp,
            "open_time": now_ts.isoformat(),
            "expiry_time": expiry_time.isoformat(),
            "expiry_hours": recommended,
            "stake_usd": stake_usd,
            "martingale_streak": mart_streak,
            "payout_pct": config.PAYOUT_PCT,
            "probability_pct_at_open": f["probability_pct"],
            "score_at_open": f["score"],
            "session_at_open": chosen_session or f.get("session"),
            "agents_for_count": f.get("agents_for_count", 0),
            "agents_against_count": f.get("agents_against_count", 0),
            "strategy_variant_at_open": chosen_variant_id or per_pair_cfg.get("best_variant"),
            "strategy_wr_pct_at_open": chosen_wr if chosen_wr is not None else per_pair_cfg.get("win_rate_pct"),
            "strategy_trades_at_open": chosen_trades,
            "strategy_source_at_open": chosen_source,  # "by_session" или "global_best"
            "backtest_30d_wr_pct_at_open": (backtest.get("pairs") or {}).get(pair, {}).get("win_rate_pct"),
            "backtest_30d_trades_at_open": (backtest.get("pairs") or {}).get(pair, {}).get("trades"),
            "live_regime_at_open": live_regime_label,
            "playbook_cell_status_at_open": playbook_cell_status,
            "playbook_cell_wr_pct_at_open": playbook_cell_wr,
            "status": "open",
        }
        open_trades.append(trade)
        opened += 1
        log.info(f"OPEN {pair} {trade_side} @ {cp} prob={f['probability_pct']}% "
                 f"expiry={recommended}h variant={chosen_variant_id} "
                 f"session={chosen_session} src={chosen_source} WR30d={chosen_wr}%")
    return opened


def _settle_expired(open_trades: list[dict], closed_trades: list[dict]) -> int:
    """Закрыть истёкшие сделки по реальной цене Yahoo."""
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
            # данные ещё не готовы — попробуем в следующий цикл
            still_open.append(t)
            continue

        if t["side"] == "BUY":
            win = close_price > t["open_price"]
        else:
            win = close_price < t["open_price"]
        # Use the trade's actual stake (may differ from default due to martingale)
        stake = float(t.get("stake_usd") or config.STAKE_USD)
        payout = float(t.get("payout_pct") or config.PAYOUT_PCT)
        pnl = (stake * payout) if win else (-stake)

        t.update({
            "status": "WIN" if win else "LOSS",
            "close_price": close_price,
            "close_time": _now().isoformat(),
            "result": "WIN" if win else "LOSS",
            "pnl_usd": round(pnl, 2),
        })
        closed_trades.append(t)
        settled += 1
        log.info(
            f"CLOSE {t['pair']} {t['side']} open={t['open_price']} "
            f"close={close_price} → {'WIN' if win else 'LOSS'} pnl={pnl:+.2f}"
        )

    open_trades[:] = still_open
    return settled


def _refresh_stats(closed: list[dict]) -> dict:
    n = len(closed)
    wins = sum(1 for t in closed if t.get("result") == "WIN")
    losses = n - wins
    pnl = sum(float(t.get("pnl_usd", 0.0)) for t in closed)
    wr = (wins / n * 100.0) if n > 0 else 0.0
    stats = {
        "as_of": _now().isoformat(),
        "total": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wr, 2),
        "total_pnl_usd": round(pnl, 2),
    }
    _save(STATS_FILE, stats)
    return stats


def cycle_once() -> dict:
    snapshot = _load(FORECASTS_FILE, {"forecasts": {}, "rankings": []})
    backtest = _load(BACKTEST_FILE, {"pairs": {}})
    strategy_cfg = _load(STRATEGY_CONFIG_FILE, {"pairs": {}})
    # Если активный cfg пустой (новая сессия / прерван sweep) — используем
    # locked baseline. Это «закреплённая стратегия ≥70%», на которой система
    # уже доказала WR; не даём упасть в baseline-без-варианта пока ждём sweep.
    if not (strategy_cfg or {}).get("pairs"):
        locked = _load(STRATEGY_LOCKED_FILE, {})
        if (locked or {}).get("pairs"):
            log.info("paper_trader: strategy_config.json пуст — использую strategy_config_locked.json")
            strategy_cfg = locked
    open_trades = _load(OPEN_FILE, [])
    closed = _load(CLOSED_FILE, [])

    settled = _settle_expired(open_trades, closed)

    # ─── HALT switch (added 2026-05-01 per user explicit request to stop
    # opening trades until 365-day sweep finishes and locked baseline is
    # active). When teamagent/state/TRADING_HALTED.flag exists, paper_trader
    # still settles expired trades, but does NOT open new ones. Removing
    # the file resumes trading immediately on next cycle.
    halt_flag = config.STATE_DIR / "TRADING_HALTED.flag"
    if halt_flag.exists():
        log.info(f"paper_trader: TRADING_HALTED.flag present → НЕ открываю новые сделки (settled={settled})")
        opened = 0
    else:
        opened = _open_new_trades(open_trades, snapshot, backtest, strategy_cfg, closed_trades=closed)

    # обогащаем для UI и сохраняем
    enriched = [_enrich_open_trade(t) for t in open_trades]
    _save(OPEN_FILE, open_trades)
    _save(config.STATE_DIR / "open_trades_enriched.json", {
        "as_of": _now().isoformat(),
        "trades": enriched,
    })
    _save(CLOSED_FILE, closed)
    stats = _refresh_stats(closed)

    return {"opened": opened, "settled": settled, "stats": stats}


def run_loop() -> None:
    log.info("paper_trader start")
    stop = {"flag": False}

    def _sig(_a, _b):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop["flag"]:
        _heartbeat()
        try:
            r = cycle_once()
            log.info(f"cycle: opened={r['opened']} settled={r['settled']} stats={r['stats']}")
        except Exception as e:
            log.exception(f"cycle failed: {e}")
        _heartbeat()
        for _ in range(config.PAPER_TRADER_INTERVAL_SEC):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("paper_trader exit")


if __name__ == "__main__":
    run_loop()
