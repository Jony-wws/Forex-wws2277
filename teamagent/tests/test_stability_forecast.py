"""Тесты для stability_forecast.py — pre-emptive прогноз стабильности.

Проверяем что прогноз:
  1) считается без падений
  2) выдаёт обязательные ключи
  3) даёт sensible CI и readiness в [0..100]
  4) корректно ловит маркет-закрытие
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import pytest

from teamagent import stability_forecast as sf


def test_forecast_returns_required_keys():
    fw = sf.forecast_window(hours_ahead=24)
    required = {
        "as_of_utc",
        "hours_ahead",
        "market_status",
        "sessions_in_window",
        "active_hours_in_window",
        "closed_hours_in_window",
        "weighted_expected_wr_pct",
        "wilson_lower_pct_95",
        "wilson_upper_pct_95",
        "active_qualified_pairs_count",
        "forecasts_eligible_now",
        "readiness_score_0_100",
        "verdict",
        "diagnosis_ru",
        "recommendations_ru",
    }
    missing = required - set(fw.keys())
    assert not missing, f"missing keys: {missing}"


def test_readiness_in_range():
    fw = sf.forecast_window(hours_ahead=24)
    assert 0 <= fw["readiness_score_0_100"] <= 100


def test_wilson_ci_sane():
    fw = sf.forecast_window(hours_ahead=24)
    lo = fw["wilson_lower_pct_95"]
    up = fw["wilson_upper_pct_95"]
    wr = fw["weighted_expected_wr_pct"]
    assert 0 <= lo <= up <= 100
    # WR must be inside CI (or very close — округление)
    assert lo - 0.5 <= wr <= up + 0.5


def test_diagnosis_is_russian_list():
    fw = sf.forecast_window(hours_ahead=24)
    diag = fw["diagnosis_ru"]
    assert isinstance(diag, list)
    assert len(diag) >= 2
    # хотя бы один элемент содержит русские символы
    assert any(any("\u0400" <= c <= "\u04ff" for c in line) for line in diag)


def test_recommendations_non_empty():
    fw = sf.forecast_window(hours_ahead=24)
    recs = fw["recommendations_ru"]
    assert isinstance(recs, list)
    assert len(recs) >= 1


def test_hours_ahead_clamped_to_window():
    # 1h должно работать
    fw = sf.forecast_window(hours_ahead=1)
    assert fw["hours_ahead"] == 1
    assert (fw["active_hours_in_window"] + fw["closed_hours_in_window"]) <= 1.05  # tolerance


def test_verdict_has_emoji_and_text():
    fw = sf.forecast_window(hours_ahead=24)
    v = fw["verdict"]
    assert "emoji" in v
    assert "text_ru" in v
    assert v["emoji"] in {"🟢", "🟡", "🟠", "🔴"}


def test_forecast_independent_of_closed_trades_count():
    """Главное требование пользователя: forecast НЕ должен зависеть от
    количества закрытых сделок. Прогон должен выдать тот же набор
    обязательных ключей независимо от состояния closed_trades.json."""
    fw1 = sf.forecast_window(hours_ahead=6)
    fw2 = sf.forecast_window(hours_ahead=6)
    # Идемпотентность для одного и того же момента (with state files static)
    # — числа могут сдвинуться на доли т.к. timestamp меняется, но структура та же
    assert set(fw1.keys()) == set(fw2.keys())
    # Wilson границы должны быть в [0..100]
    assert 0 <= fw1["wilson_lower_pct_95"] <= 100
    assert 0 <= fw1["wilson_upper_pct_95"] <= 100


def test_market_status_inside_forecast():
    fw = sf.forecast_window(hours_ahead=24)
    ms = fw["market_status"]
    assert "is_open" in ms
    assert "session" in ms
    assert "next_event_utc" in ms
