"""live_analyst — «думаю в реальном времени» per-pair narrative.

Объединяет в одну функцию:
- последний forecast для пары (probability, side, score breakdown);
- live regime classification (Hurst + ATR%);
- playbook lookup для (pair, current_session, current_regime);
- session window status (открыт ли рынок, до закрытия и т.д.);
- персональный narrative на русском, объясняющий «почему сейчас можно/нельзя».

Используется как:
- API endpoint `/api/analyst/{pair}` — возвращает JSON с narrative.
- Frontend section «🧠 ЖИВОЙ AI-АНАЛИТИК — мысли в реальном времени» крутится
  по всем 28 парам каждые 30 сек.
- paper_trader при выборе variant'a сверяется с playbook через
  `lookup_playbook_cell()`.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config, regime, strategies
from .data import yahoo


PLAYBOOK_FILE = config.STATE_DIR / "playbook.json"
FORECASTS_FILE = config.STATE_DIR / "forecasts.json"


def _read_json_safe(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _current_session_utc(now: datetime | None = None) -> str | None:
    """Какая канон. сессия в текущий момент UTC. None если 22-23 UTC (off)."""
    if now is None:
        now = datetime.now(timezone.utc)
    return strategies.detect_session(now.hour)


def _live_regime(pair: str) -> dict:
    """Возвращает текущий режим пары на 1H барах за последний месяц."""
    try:
        bars = yahoo.fetch(pair, interval="1h", period="3mo")
    except Exception:
        bars = None
    if bars is None or bars.empty:
        return {
            "regime": "mean_reverting",
            "hurst": 0.5,
            "atr_pct": 0.0,
            "atr_pct_percentile": 50.0,
            "ema_stack": "neutral",
            "label_ru": "нет данных",
            "n_bars": 0,
        }
    return regime.regime_summary(bars, lookback=200)


def lookup_playbook_cell(pair: str, session: str, current_regime: str,
                         playbook: dict | None = None) -> dict | None:
    """Возвращает запись playbook для (pair, session, regime).

    None если playbook не сгенерирован или ячейка отсутствует / INSUFFICIENT.
    """
    if playbook is None:
        playbook = _read_json_safe(PLAYBOOK_FILE, {})
    p = playbook.get("pairs", {}).get(pair, {})
    sess = p.get("sessions", {}).get(session, {})
    cell = sess.get("regimes", {}).get(current_regime)
    return cell


def _last_forecast(pair: str) -> dict | None:
    f = _read_json_safe(FORECASTS_FILE, {})
    pairs = f.get("pairs", {}) if isinstance(f, dict) else {}
    return pairs.get(pair)


def _verdict_emoji_and_text(cell: dict | None, prob_pct: float | None) -> tuple[str, str]:
    if prob_pct is None or cell is None:
        return "🟡", "размышляю — не хватает данных"
    if cell.get("status") == "STORM_PROOF":
        return "🟢", f"open trade: режим storm-proof, WR={cell['wr_pct']}%"
    if cell.get("status") == "QUALIFIED":
        return "🟢", f"open trade: qualified, WR={cell['wr_pct']}% (Wilson≥{cell['wilson_lower_pct']}%)"
    if cell.get("status") == "PROBABLE":
        return "🟡", f"осторожно: probable, WR={cell['wr_pct']}% (мало уверенности)"
    if cell.get("status") == "INSUFFICIENT":
        return "🟡", "мало исторических сделок в этом регионе режима"
    return "🔴", "режим не подходит — пропускаем"


def _narrative_ru(pair: str, session: str | None, live_regime: dict,
                  forecast: dict | None, cell: dict | None) -> str:
    rgm_label = live_regime.get("label_ru", "режим неизвестен")
    h = live_regime.get("hurst", 0.5)
    atr_pp = live_regime.get("atr_pct_percentile", 50.0)
    ema = live_regime.get("ema_stack", "neutral")

    if session is None:
        return (
            f"{pair}: рынок сейчас вне канон. сессии (22-23 UTC). "
            f"Режим — {rgm_label} (H={h:.2f}). Жду открытия Asia в 00:00 UTC."
        )
    if forecast is None:
        return (
            f"{pair}: нет свежего прогноза. Сессия {session}, режим {rgm_label} "
            f"(H={h:.2f}, ATR%-percentile={atr_pp:.0f})."
        )
    side = forecast.get("side", "?")
    prob = forecast.get("probability_pct", 0)
    cell_part = ""
    if cell is None:
        cell_part = f"для ячейки ({pair}, {session}, {live_regime.get('regime', '?')}) playbook ещё не построен — fallback на свободный 70%-gate"
    elif cell.get("status") == "STORM_PROOF":
        cell_part = (
            f"эта ячейка прошла worst-30d стресс-тест "
            f"(WR={cell['wr_pct']}%, Wilson≥{cell['wilson_lower_pct']}%, side_bias={cell.get('side_bias')}, "
            f"n={cell['n_trades']}) — открываем сделку."
        )
    elif cell.get("status") == "QUALIFIED":
        cell_part = (
            f"ячейка qualified (WR={cell['wr_pct']}%, n={cell['n_trades']}); "
            f"в кризис WR проседает до {cell.get('worst_30d_wr_pct')}% — открываем сделку."
        )
    elif cell.get("status") == "PROBABLE":
        cell_part = (
            f"ячейка probable (WR={cell['wr_pct']}%, n={cell['n_trades']}) — "
            f"меньше уверенности, открываем со штатным гейтом 70%."
        )
    elif cell.get("status") == "INSUFFICIENT":
        cell_part = (
            f"в этом режиме за 365 дней набралось только {cell['n_trades']} сделок — "
            f"данных мало, fallback на свободный 70%-gate."
        )
    else:  # FROZEN
        cell_part = (
            f"ячейка FROZEN (WR={cell['wr_pct']}%, n={cell['n_trades']}) — "
            f"исторически слабая, в этом режиме НЕ открываем."
        )
    return (
        f"{pair}: сессия {session}. Режим — {rgm_label} (H={h:.2f}, "
        f"ATR%-perc={atr_pp:.0f}, EMA-stack={ema}). Прогноз = {side} {prob}%. {cell_part}"
    )


def live_analyst(pair: str, *, playbook: dict | None = None, now: datetime | None = None) -> dict:
    """Главная функция — собирает «мысли в реальном времени» по паре.

    Возвращает dict с полями:
      pair, as_of, session, live_regime, forecast (subset),
      playbook_cell, verdict, narrative_ru.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if playbook is None:
        playbook = _read_json_safe(PLAYBOOK_FILE, {})

    session = _current_session_utc(now)
    rgm = _live_regime(pair)
    forecast = _last_forecast(pair)
    cell = None
    if session and rgm.get("regime"):
        cell = lookup_playbook_cell(pair, session, rgm["regime"], playbook=playbook)

    prob_pct = (forecast or {}).get("probability_pct")
    verdict_emoji, verdict_text = _verdict_emoji_and_text(cell, prob_pct)
    narrative = _narrative_ru(pair, session, rgm, forecast, cell)

    fc_subset = None
    if forecast:
        fc_subset = {
            "side": forecast.get("side"),
            "probability_pct": forecast.get("probability_pct"),
            "score": forecast.get("score"),
            "expiry_recommended_h": forecast.get("expiry_recommended_h"),
            "scanned_at": forecast.get("scanned_at"),
        }

    return {
        "pair": pair,
        "as_of": now.isoformat(),
        "session": session,
        "live_regime": rgm,
        "forecast": fc_subset,
        "playbook_cell": cell,
        "verdict_emoji": verdict_emoji,
        "verdict_text": verdict_text,
        "narrative_ru": narrative,
    }


def live_analyst_all() -> list[dict]:
    """Запускает live_analyst по всем 28 парам — для пакетной выдачи.

    Кэширует playbook раз на вызов (не читаем JSON 28 раз).
    """
    playbook = _read_json_safe(PLAYBOOK_FILE, {})
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for pair in config.PAIRS:
        try:
            out.append(live_analyst(pair, playbook=playbook, now=now))
        except Exception as e:
            out.append({
                "pair": pair,
                "as_of": now.isoformat(),
                "error": str(e),
                "narrative_ru": f"{pair}: ошибка анализа — {e}",
            })
    return out
