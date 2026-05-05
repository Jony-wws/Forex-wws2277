"""Конфигурация TeamAgent.

28 валютных пар (как в прошлой сессии), торговые сессии, кэпы и тайминги.
"""
from __future__ import annotations
import os
from pathlib import Path
from datetime import timedelta

# ───── корневые директории ─────
ROOT = Path(__file__).resolve().parent
# Allow override via env (Fly.io persistent volume at /data); fallback to ROOT.
STATE_DIR = Path(os.environ.get("TEAMAGENT_STATE_DIR", ROOT / "state"))
LOGS_DIR = Path(os.environ.get("TEAMAGENT_LOGS_DIR", ROOT / "logs"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ───── 28 валютных пар (Yahoo Finance тикеры) ─────
# Major + cross + JPY-pairs
PAIRS: list[str] = [
    # majors
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    # EUR crosses
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    # GBP crosses
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    # JPY crosses
    "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY",
    # other crosses
    "AUDCAD", "AUDCHF", "AUDNZD",
    "CADCHF", "NZDCAD", "NZDCHF",
]
assert len(PAIRS) == 28, f"need exactly 28 pairs, got {len(PAIRS)}"

# Yahoo Finance ticker mapping (FX needs '=X' suffix)
def yahoo_ticker(pair: str) -> str:
    """EURUSD -> EURUSD=X."""
    return f"{pair}=X"

# ───── торговые сессии (UTC) ─────
SESSIONS: dict[str, tuple[int, int]] = {
    "Asia": (0, 6),
    "London": (7, 11),
    "LON+NY": (12, 15),
    "NY": (16, 21),
}

# ───── параметры paper-trader ─────
# 2026-05-01: пользователь явно попросил мартингейл-стратегию с шагом
# $1 → $2 → $4. STAKE_USD теперь = базовая ставка ($1). Реальный размер
# открываемой сделки определяет mart_engine ниже (после loss-стрика).
STAKE_USD = 1.0
PAYOUT_PCT = 0.85          # WIN +$0.85 на $1, LOSS -$1 (paper-trader simulation)
# Phase 11 (2026-05-05): user's REAL broker pays 70% on a winning binary, not 85%.
# We expose `BROKER_PAYOUT_PCT` as the displayed-EV reference so the dashboard
# can show whether each forecast actually has POSITIVE math expectation at the
# user's broker payout. Override via env: `export BROKER_PAYOUT_PCT=0.85`.
# At 70% payout, break-even WR = 1 / (1+0.70) = 58.82%. Below that → losing on
# distance. Above 70% probability with realized cell WR ≥ 70% → +EV with margin.
BROKER_PAYOUT_PCT = float(os.environ.get("BROKER_PAYOUT_PCT", "0.70"))
MIN_PROBABILITY = 0.70     # открываем только если ≥70%
MAX_PROBABILITY = 0.92     # кэп — никогда не показываем 100%

# Мартингейл (2026-05-01 user request): после N подряд LOSS на ОДНОЙ паре
# следующая ставка умножается на MARTINGALE_MULT^streak. Сбрасывается
# после первой WIN. Cap = MARTINGALE_MAX_STREAK чтобы не разогнаться.
MARTINGALE_ENABLED = True
MARTINGALE_MULT = 2.0
MARTINGALE_MAX_STREAK = 3       # 1$ → 2$ → 4$, дальше — резет к 1$ независимо от исхода

# 2026-05-01 user request: STRICT-режим — открываем сделки ТОЛЬКО когда
# (pair, current_session) попадает в список qualified cells (≥70% WR на
# 365д бэктесте). Baseline-fallback ОТКЛЮЧЁН. Цель: выдержать ≥70% WR на
# каждой реальной сделке. Если ни одной qualified ячейки в текущий момент
# нет — paper_trader просто не откроет сделку (ждём следующего часа).
STRICT_QUALIFIED_GATE = True
DEFAULT_EXPIRY_HOURS = 2   # если recommended_hours не указан
MIN_EXPIRY_HOURS = 1
# 2026-05-03: Ichimoku и ADX-trend варианты лучше держать чуть дольше
# (5 часов), чтобы тренд успел реализоваться. Было 4.
MAX_EXPIRY_HOURS = 5

# ───── Ensemble voting (2026-05-03 user request) ─────
# Ensemble складывает голоса top_variants (до 10) для каждой (pair, session)
# ячейки. Сделка открывается ТОЛЬКО если ≥80% вариантов согласны (4/5, 3/3,
# 2/2). Эффективный WR на ячейке поднимается на ~10-15% за счёт фильтрации
# одиночных false-сигналов.
ENSEMBLE_ENABLED = True
ENSEMBLE_MIN_VARIANTS = 2          # минимум вариантов для ensemble (иначе fallback)
ENSEMBLE_MIN_AGREEMENT_PCT = 80    # 80% вариантов должны согласиться
ENSEMBLE_MIN_VARIANT_WR = 65.0     # минимум WR варианта (%) чтобы участвовать в голосовании
ENSEMBLE_MIN_VARIANT_TRADES = 8    # минимум сделок варианта на 365д для статзначимости

# ───── Корреляционный фильтр (2026-05-03) ─────
# Не открываем больше N сделок одновременно на одну базовую валюту (EUR/GBP/
# JPY/...). Защита от кластерных убытков на одном макро-шоке.
MAX_SAME_CURRENCY_BLOCK = 2

# ───── циклы ─────
FORECAST_SCANNER_INTERVAL_SEC = 5 * 60      # 5 мин — обход всех 28 пар
PAPER_TRADER_INTERVAL_SEC = 60              # 1 мин — открытие/закрытие сделок
DASHBOARD_REFRESH_SEC = 30                  # 30 сек — обновление UI
WATCHDOG_INTERVAL_SEC = 60                  # 60 сек — heartbeat-чек
AGENT_DEAD_AFTER_SEC = 10 * 60              # 10 мин без heartbeat → kill+restart

# ───── индикаторы ─────
ATR_PERIOD = 14
RSI_PERIOD = 14
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
BB_PERIOD = 20
BB_STD = 2.0
MOMENTUM_LOOKBACK = 5

# ───── штрафы / penalties ─────
NEWS_BLACKOUT_PENALTY = 5  # на сколько уменьшаем abs(score) при high-impact новости ±30 мин

# ───── Volume Profile (Стакан) ─────
VP_BARS = 720           # 720 1-мин баров = 12 часов
VP_BUCKETS = 50         # цену делим на 50 уровней
VP_BIG_PLAYER_PCTL = 80 # уровни ≥80-го перцентиля = крупные игроки

# ───── UTC+5 граница (для прогноза «куда цена не вернётся») ─────
UTC_OFFSET_HOURS = 5   # UTC+5 (Mscow+2 / Yekaterinburg)

# ───── LLM провайдеры ─────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DERIV_DEMO_TOKEN = os.environ.get("DERIV_DEMO_TOKEN", "")

# ───── список моделей ─────
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]
GEMINI_MODELS = [
    "gemini-2.0-flash-exp",
    "gemini-1.5-flash",
]
OPENROUTER_MODELS = [
    "deepseek/deepseek-chat",
    "qwen/qwen-2.5-72b-instruct:free",
]

# ───── серверы / тоннели ─────
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
