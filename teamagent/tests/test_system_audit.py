"""Tests for system_audit.

Two main classes of tests:
  1. SMOKE — full audit on real current state must not crash, and must
     return a valid envelope with 6 categories and ≥15 checks.
  2. UNIT — each individual check function returns a valid record
     {status, message_ru, details} without raising.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from teamagent import system_audit as sa


# ───── helpers ─────

VALID_STATUSES = {"green", "yellow", "red"}


def _assert_check_envelope(result: dict, name: str) -> None:
    assert isinstance(result, dict), f"{name}: not a dict"
    assert result["status"] in VALID_STATUSES, f"{name}: bad status {result['status']}"
    assert isinstance(result["message_ru"], str), f"{name}: message_ru must be str"
    assert isinstance(result["details"], dict), f"{name}: details must be dict"


# ───── smoke ─────

def test_run_audit_smoke():
    """Full audit doesn't raise and returns valid envelope."""
    r = sa.run_audit()
    assert "as_of_utc" in r
    assert r["overall_status"] in VALID_STATUSES
    assert r["summary"]["total"] >= 15
    assert r["summary"]["green"] + r["summary"]["yellow"] + r["summary"]["red"] == r["summary"]["total"]
    assert isinstance(r["categories"], list) and len(r["categories"]) == 6
    assert isinstance(r["verdict_ru"], str) and len(r["verdict_ru"]) > 5
    assert isinstance(r["recommendations_ru"], list)


def test_each_category_has_checks():
    r = sa.run_audit()
    for cat in r["categories"]:
        assert isinstance(cat["checks"], list) and len(cat["checks"]) >= 1
        assert "label_ru" in cat
        for chk in cat["checks"]:
            _assert_check_envelope(chk, chk.get("name", "?"))


def test_summary_color_count_matches_checks():
    r = sa.run_audit()
    cnt = {"green": 0, "yellow": 0, "red": 0}
    for cat in r["categories"]:
        for chk in cat["checks"]:
            cnt[chk["status"]] += 1
    assert cnt["green"] == r["summary"]["green"]
    assert cnt["yellow"] == r["summary"]["yellow"]
    assert cnt["red"] == r["summary"]["red"]


# ───── unit checks ─────

def test_paper_stats_consistency_check():
    """paper_stats vs closed_trades check works end-to-end."""
    res = sa._safe_check("test", sa._chk_paper_stats_vs_closed_trades)
    _assert_check_envelope(res, "paper_stats")
    # On real state should be green (we just fixed it during audit run)
    if res["status"] != "green":
        pytest.fail(f"expected green but got {res['status']}: {res['message_ru']}")


def test_stakan_stats_internal_consistency():
    res = sa._safe_check("stakan", sa._chk_stakan_stats_vs_state)
    _assert_check_envelope(res, "stakan")


def test_forecasts_cover_all_pairs():
    """After fix: every config.PAIRS pair must have entry in forecasts.json
    (placeholder for skipped pairs)."""
    res = sa._safe_check("forecasts", sa._chk_forecasts_cover_all_pairs)
    _assert_check_envelope(res, "forecasts")
    assert res["status"] == "green", res["message_ru"]


def test_qualified_pairs_match():
    res = sa._safe_check("qualified", sa._chk_qualified_pairs_match)
    _assert_check_envelope(res, "qualified")
    assert res["status"] == "green", res["message_ru"]


def test_market_session_consistent():
    res = sa._safe_check("session", sa._chk_market_hours_consistent_with_session)
    _assert_check_envelope(res, "session")
    assert res["status"] == "green", res["message_ru"]


def test_open_trades_within_market():
    res = sa._safe_check("open", sa._chk_open_trades_within_market)
    _assert_check_envelope(res, "open")


def test_closed_trades_pnl_consistency():
    """PnL formula must match either fraction (0.85) or percent (85)
    payout convention."""
    res = sa._safe_check("pnl", sa._chk_closed_trades_pnl_payout_consistency)
    _assert_check_envelope(res, "pnl")
    assert res["status"] == "green", res["message_ru"]


def test_state_files_exist():
    res = sa._safe_check("exist", sa._chk_required_state_files_exist)
    _assert_check_envelope(res, "exist")
    assert res["status"] == "green"


def test_state_schemas():
    res = sa._safe_check("schema", sa._chk_state_schemas)
    _assert_check_envelope(res, "schema")
    assert res["status"] == "green", res["message_ru"]


def test_freshness_ok_or_warn():
    res = sa._safe_check("fresh", sa._chk_freshness)
    _assert_check_envelope(res, "fresh")
    # Only green/yellow acceptable in test (red = scanner stopped >1h)
    assert res["status"] in ("green", "yellow"), res["message_ru"]


def test_code_compiles_clean():
    res = sa._safe_check("compile", sa._chk_code_compiles)
    _assert_check_envelope(res, "compile")
    assert res["status"] == "green", res["message_ru"]


def test_critical_imports_work():
    res = sa._safe_check("imports", sa._chk_critical_imports)
    _assert_check_envelope(res, "imports")
    assert res["status"] == "green", res["message_ru"]


def test_config_sanity():
    res = sa._safe_check("config", sa._chk_config_sanity)
    _assert_check_envelope(res, "config")
    assert res["status"] == "green", res["message_ru"]


def test_adaptive_expiry_consistent():
    res = sa._safe_check("expiry", sa._chk_adaptive_expiry_consistent)
    _assert_check_envelope(res, "expiry")
    assert res["status"] == "green", res["message_ru"]


def test_market_close_buffer_sane():
    res = sa._safe_check("buffer", sa._chk_market_hours_buffer_sane)
    _assert_check_envelope(res, "buffer")
    assert res["status"] == "green", res["message_ru"]


# ───── corruption-detection ─────

def test_audit_detects_intentional_paper_stats_corruption(tmp_path, monkeypatch):
    """Corrupt paper_stats and verify _chk_paper_stats_vs_closed_trades
    surfaces RED."""
    state_dir = sa._STATE_DIR
    backup = json.loads((state_dir / "paper_stats.json").read_text())
    try:
        # Bump "wins" by 100 so totals diverge from closed_trades
        bad = dict(backup)
        bad["wins"] = backup["wins"] + 100
        (state_dir / "paper_stats.json").write_text(json.dumps(bad, indent=2))
        res = sa._safe_check("test", sa._chk_paper_stats_vs_closed_trades)
        assert res["status"] == "red", "audit didn't catch wins-mismatch"
    finally:
        (state_dir / "paper_stats.json").write_text(json.dumps(backup, indent=2))


def test_audit_detects_missing_required_key(tmp_path):
    """Corrupt schema by removing a required key."""
    state_dir = sa._STATE_DIR
    p = state_dir / "paper_stats.json"
    backup = json.loads(p.read_text())
    try:
        bad = dict(backup)
        bad.pop("total", None)
        p.write_text(json.dumps(bad, indent=2))
        res = sa._safe_check("test", sa._chk_state_schemas)
        assert res["status"] == "red", "schema check missed missing total"
        # Also paper_stats vs closed_trades should be RED (total != len(ct))
        # but we already corrupted paper_stats, so safer to skip that here.
    finally:
        p.write_text(json.dumps(backup, indent=2))


def test_safe_check_catches_exceptions():
    """If a checker raises, _safe_check returns RED with traceback."""
    def boom():
        raise ValueError("kaboom")
    out = sa._safe_check("boom_test", boom)
    assert out["status"] == "red"
    assert "kaboom" in out["message_ru"] or "ValueError" in out["message_ru"]
    assert out["name"] == "boom_test"
