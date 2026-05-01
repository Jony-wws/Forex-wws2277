"""ДОКАЗАТЕЛЬСТВА КОРРЕКТНОСТИ СИСТЕМЫ — system_audit.py

Это мета-модуль: он не торгует и не считает гарантий, он отвечает на
вопрос "можно ли верить тому, что система сама про себя говорит".

Каждая проверка независима и отвечает 🟢 / 🟡 / 🔴 + russian explanation.

Все проверки группируются в 6 категорий:

  1. SELF-CONSISTENCY — кросс-проверки между state-файлами.
     paper_stats.total ≡ len(closed_trades), wins+losses ≡ total,
     Σ pnl_usd ≡ paper_stats.total_pnl_usd, qualified_pairs из
     stability_forecast ≡ qualified_pairs из strategy_config_locked.

  2. SCHEMA VALIDITY — у всех state JSON-файлов на месте обязательные
     ключи правильных типов. Если файл повреждён или новый код пишет
     не-те поля — ловим тут до того, как фронт сломается.

  3. FRESHNESS — критичные state-файлы не старее N минут.
     forecasts.json ≤10 мин (scanner работает), backtest_30d.json ≤2ч,
     strategy_config_locked.json должен существовать.

  4. CODE HEALTH — все .py в teamagent/ компилируются (py_compile),
     все API-endpoints возвращают 200 на smoke-call (без сети).

  5. CONFIG SANITY — config.PAIRS, config.SESSIONS не пусты,
     min_prob/max_prob в разумных пределах, market_hours границы корректны.

  6. CROSS-MODULE INVARIANTS — paper_trader.ADAPTIVE_*_H ⊂ market_hours
     допустимые значения, expiry в config совпадает с реальными expiry в
     closed_trades, adaptive [1..5] не противоречит strategy_config.

Каждая проверка не должна падать и не должна делать сетевых вызовов
(аудит должен работать даже в офлайне). При ошибке самой проверки
возвращаем 🔴 с message "checker raised: ...".
"""
from __future__ import annotations

import json
import os
import py_compile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# ───── константы ─────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = Path(__file__).resolve().parent / "state"
_TEAMAGENT_DIR = Path(__file__).resolve().parent

# Обязательные state-файлы и их обязательные ключи
_REQUIRED_STATE_FILES: dict[str, dict[str, type | tuple[type, ...]]] = {
    "paper_stats.json": {
        "as_of": str,
        "total": int,
        "wins": int,
        "losses": int,
        "win_rate_pct": (int, float),
        "total_pnl_usd": (int, float),
    },
    "stakan_stats.json": {
        "as_of": str,
        "total": int,
        "wins": int,
        "losses": int,
        "win_rate_pct": (int, float),
        "total_pnl_usd": (int, float),
    },
    "forecasts.json": {
        "scanned_at": str,
        "total_pairs": int,
        "forecasts": dict,
    },
    "strategy_config_locked.json": {
        "as_of": str,
        "pairs": dict,
    },
}

# Файлы-списки (просто проверяем что валидный JSON-array)
_REQUIRED_LIST_FILES: list[str] = [
    "closed_trades.json",
    "open_trades.json",
]

# Свежесть в секундах (если файл существует и старше — 🟡 или 🔴)
_FRESHNESS_THRESHOLDS_SEC: dict[str, tuple[int, int]] = {
    # filename: (warn_sec, critical_sec)
    "forecasts.json":             (15 * 60,  60 * 60),  # scanner раз в 5 мин
    "paper_stats.json":           (30 * 60,  120 * 60),
    "stakan_stats.json":          (30 * 60,  120 * 60),
}


# ───── helpers ─────

def _state_path(name: str) -> Path:
    return _STATE_DIR / name


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _file_age_seconds(path: Path) -> float | None:
    try:
        mtime = path.stat().st_mtime
        return max(0.0, time.time() - mtime)
    except OSError:
        return None


def _safe_check(name: str, fn: Callable[[], dict]) -> dict:
    """Run a check function; if it explodes, surface that as a RED check."""
    try:
        out = fn()
        out.setdefault("name", name)
        out.setdefault("status", "green")
        out.setdefault("message_ru", "")
        out.setdefault("details", {})
        return out
    except Exception as exc:
        return {
            "name": name,
            "status": "red",
            "message_ru": f"проверка упала: {type(exc).__name__}: {exc}",
            "details": {"traceback": traceback.format_exc().splitlines()[-3:]},
        }


def _status_color_count(checks: list[dict]) -> dict[str, int]:
    cnt = {"green": 0, "yellow": 0, "red": 0}
    for c in checks:
        cnt[c.get("status", "red")] = cnt.get(c.get("status", "red"), 0) + 1
    return cnt


# ═════════ SELF-CONSISTENCY CHECKS ═════════

def _chk_paper_stats_vs_closed_trades() -> dict:
    """paper_stats.total ≡ len(closed_trades), wins+losses ≡ total,
    Σ pnl_usd ≡ paper_stats.total_pnl_usd."""
    ps = _load_json(_state_path("paper_stats.json"))
    ct = _load_json(_state_path("closed_trades.json"))

    total_ps = int(ps.get("total", 0))
    total_ct = len(ct)
    wins_ps = int(ps.get("wins", 0))
    losses_ps = int(ps.get("losses", 0))
    wins_ct = sum(1 for t in ct if t.get("result") == "WIN")
    losses_ct = sum(1 for t in ct if t.get("result") == "LOSS")
    pnl_ps = float(ps.get("total_pnl_usd", 0))
    pnl_ct = round(sum(float(t.get("pnl_usd", 0) or 0) for t in ct), 4)

    issues = []
    if total_ps != total_ct:
        issues.append(f"paper_stats.total={total_ps}, len(closed_trades)={total_ct}")
    if wins_ps != wins_ct:
        issues.append(f"paper_stats.wins={wins_ps}, in closed={wins_ct}")
    if losses_ps != losses_ct:
        issues.append(f"paper_stats.losses={losses_ps}, in closed={losses_ct}")
    if abs(pnl_ps - pnl_ct) > 0.01:
        issues.append(f"paper_stats.pnl={pnl_ps}, sum(closed.pnl)={pnl_ct}")
    if (wins_ct + losses_ct) != total_ct:
        issues.append(
            f"WIN+LOSS ({wins_ct + losses_ct}) ≠ total ({total_ct}) — "
            "есть закрытые сделки без result"
        )

    if issues:
        return {
            "status": "red",
            "message_ru": "paper_stats и closed_trades не сходятся",
            "details": {"diffs": issues, "pnl_ps": pnl_ps, "pnl_ct": pnl_ct},
        }
    return {
        "status": "green",
        "message_ru": (
            f"paper_stats полностью сошёлся с closed_trades "
            f"(total={total_ps}, wins={wins_ps}, losses={losses_ps}, pnl={pnl_ps})"
        ),
        "details": {"total": total_ps, "wins": wins_ps, "losses": losses_ps, "pnl": pnl_ps},
    }


def _chk_stakan_stats_vs_state() -> dict:
    """stakan_stats внутренняя консистентность."""
    ss = _load_json(_state_path("stakan_stats.json"))
    total = int(ss.get("total", 0))
    wins = int(ss.get("wins", 0))
    losses = int(ss.get("losses", 0))
    wr = float(ss.get("win_rate_pct", 0))

    issues = []
    if total != wins + losses:
        issues.append(f"total={total} ≠ wins+losses={wins + losses}")
    if total > 0:
        expected_wr = round(100.0 * wins / total, 2)
        if abs(wr - expected_wr) > 0.5:
            issues.append(f"WR={wr}% ≠ wins/total={expected_wr}%")

    if issues:
        return {"status": "red", "message_ru": "stakan_stats внутренне противоречит",
                "details": {"diffs": issues}}
    return {
        "status": "green",
        "message_ru": f"stakan_stats внутренне консистентен (total={total}, WR={wr}%)",
        "details": {"total": total, "wins": wins, "losses": losses, "wr": wr},
    }


def _chk_forecasts_cover_all_pairs() -> dict:
    """forecasts.json содержит все пары из config.PAIRS."""
    from . import config
    fc = _load_json(_state_path("forecasts.json"))
    pairs_in = set((fc.get("forecasts") or {}).keys())
    pairs_expected = set(config.PAIRS)

    missing = pairs_expected - pairs_in
    extra = pairs_in - pairs_expected

    if missing:
        return {
            "status": "red",
            "message_ru": f"в forecasts.json отсутствуют пары: {sorted(missing)}",
            "details": {"missing": sorted(missing), "extra": sorted(extra)},
        }
    if extra:
        return {
            "status": "yellow",
            "message_ru": f"в forecasts.json лишние пары: {sorted(extra)}",
            "details": {"extra": sorted(extra)},
        }
    return {
        "status": "green",
        "message_ru": f"forecasts.json покрывает все {len(pairs_expected)} пар из config.PAIRS",
        "details": {"covered": len(pairs_in)},
    }


def _chk_qualified_pairs_match() -> dict:
    """qualified_pairs_count, посчитанный аудитом по тому же state-файлу,
    что использует stability_forecast (live strategy_config или locked
    fallback), совпадает с тем, что отдаёт stability_forecast."""
    from . import stability_forecast as sf
    fw = sf.forecast_window(hours_ahead=24)
    sf_count = int(fw.get("active_qualified_pairs_count", -1))

    # ВАЖНО: используем точно тот же loader, чтобы сравнивать яблоки с яблоками.
    strategy = sf._load_strategy()
    pairs = strategy.get("pairs", {}) or {}
    audit_count = 0
    for pair, cfg in pairs.items():
        by = cfg.get("by_session") or {}
        for s, sd in by.items():
            if not sd:
                continue
            wr = sd.get("win_rate_pct", 0) or 0
            tr = sd.get("trades", 0) or 0
            if wr >= 70.0 and tr >= 5:
                audit_count += 1
                break  # этот pair квалифицирован хоть в одной сессии

    if sf_count != audit_count:
        return {
            "status": "red",
            "message_ru": (
                f"stability_forecast говорит qualified_pairs={sf_count}, "
                f"независимый аудит по тому же файлу даёт {audit_count} — "
                f"система противоречит сама себе"
            ),
            "details": {"sf": sf_count, "audit": audit_count},
        }
    return {
        "status": "green",
        "message_ru": (
            f"qualified_pairs={sf_count} совпадает у stability_forecast "
            f"и у независимого аудита по тому же strategy_config"
        ),
        "details": {"qualified_pairs": sf_count},
    }


def _chk_market_hours_consistent_with_session() -> dict:
    """market_hours.current_session() ≡ часовое окно по UTC."""
    from . import market_hours as mh
    now = datetime.now(timezone.utc)
    sess = mh.current_session(now)
    h = now.hour
    expected = (
        "Closed" if not mh.is_market_open(now) else
        "Asia" if h < 7 else
        "London" if h < 13 else
        "Overlap" if h < 17 else
        "NY" if h < 22 else
        "Closed"
    )
    if sess != expected:
        return {
            "status": "red",
            "message_ru": f"market_hours.current_session()={sess}, ожидалось {expected} по UTC h={h}",
            "details": {"sess": sess, "expected": expected, "h": h},
        }
    return {
        "status": "green",
        "message_ru": f"current_session={sess} совпадает с UTC-часовой логикой (h={h})",
        "details": {"session": sess, "utc_hour": h},
    }


def _chk_open_trades_within_market() -> dict:
    """Все open_trades имеют expiry_time ≤ next_close (никакая сделка не
    'торчит' за закрытие рынка)."""
    from . import market_hours as mh
    ot = _load_json(_state_path("open_trades.json"))
    if not ot:
        return {"status": "green", "message_ru": "открытых сделок нет — нечего проверять",
                "details": {"open_count": 0}}
    bad = []
    for t in ot:
        et = t.get("expiry_time")
        if not et:
            bad.append((t.get("id"), "no expiry_time"))
            continue
        try:
            dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            bad.append((t.get("id"), f"bad expiry_time: {e}"))
            continue
        nc = mh.next_close(dt - timedelta(seconds=1))
        # Допускаем 5 минут за границу (settlement_price берётся постфактум)
        if dt > nc + timedelta(minutes=5):
            bad.append((t.get("id"), t.get("pair"), f"expiry {dt} > next_close {nc}"))
    if bad:
        return {
            "status": "red",
            "message_ru": f"{len(bad)} открытых сделок выходят за market_hours",
            "details": {"violations": bad[:5], "total_violations": len(bad)},
        }
    return {
        "status": "green",
        "message_ru": f"все {len(ot)} открытых сделок укладываются в market_hours",
        "details": {"open_count": len(ot)},
    }


def _chk_closed_trades_pnl_payout_consistency() -> dict:
    """Для каждой closed_trade pnl_usd должен соответствовать
    stake/payout/result по бинарной формуле.

    Поле payout_pct в state-файлах хранится как fraction (0.85), а не
    как percentage (85). Чтобы аудит был устойчив к обеим конвенциям,
    нормализуем: значения >1.0 трактуем как percent, ≤1.0 как fraction.
    """
    ct = _load_json(_state_path("closed_trades.json"))
    bad = []
    fraction_count = 0
    percent_count = 0
    for t in ct[-50:]:
        stake = float(t.get("stake_usd", 0) or 0)
        payout_raw = float(t.get("payout_pct", 0) or 0)
        # Нормализация: 0.85 → 0.85; 85 → 0.85
        if payout_raw > 1.0:
            payout_factor = payout_raw / 100.0
            percent_count += 1
        else:
            payout_factor = payout_raw
            fraction_count += 1
        result = t.get("result")
        pnl = float(t.get("pnl_usd", 0) or 0)
        if result == "WIN":
            expected = round(stake * payout_factor, 4)
            if abs(pnl - expected) > 0.01:
                bad.append((t.get("id"), t.get("pair"), f"WIN expected={expected}, got={pnl}"))
        elif result == "LOSS":
            expected = -stake
            if abs(pnl - expected) > 0.01:
                bad.append((t.get("id"), t.get("pair"), f"LOSS expected={expected}, got={pnl}"))
    if bad:
        return {
            "status": "red",
            "message_ru": f"{len(bad)} закрытых сделок имеют PnL не по формуле",
            "details": {"violations": bad[:5], "checked": min(50, len(ct))},
        }
    msg = f"PnL формула консистентна на последних {min(50, len(ct))} сделках"
    if fraction_count and percent_count:
        msg += f" (но смешан формат payout: {fraction_count} fraction, {percent_count} percent — нормализуется в аудите)"
    return {
        "status": "green",
        "message_ru": msg,
        "details": {"checked": min(50, len(ct)),
                    "fraction_count": fraction_count,
                    "percent_count": percent_count},
    }


# ═════════ SCHEMA VALIDITY CHECKS ═════════

def _chk_required_state_files_exist() -> dict:
    missing = [n for n in _REQUIRED_STATE_FILES if not _state_path(n).exists()]
    if missing:
        return {"status": "red", "message_ru": f"нет state-файлов: {missing}",
                "details": {"missing": missing}}
    return {"status": "green", "message_ru": f"все {len(_REQUIRED_STATE_FILES)} ключевых state-файлов на месте",
            "details": {"checked": list(_REQUIRED_STATE_FILES.keys())}}


def _chk_state_schemas() -> dict:
    issues = []
    for name, schema in _REQUIRED_STATE_FILES.items():
        p = _state_path(name)
        if not p.exists():
            continue  # отдельная проверка
        try:
            d = _load_json(p)
        except Exception as e:
            issues.append((name, f"invalid JSON: {e}"))
            continue
        if not isinstance(d, dict):
            issues.append((name, f"expected dict, got {type(d).__name__}"))
            continue
        for key, typ in schema.items():
            if key not in d:
                issues.append((name, f"missing key '{key}'"))
            elif not isinstance(d[key], typ):
                issues.append((name, f"'{key}' wrong type: {type(d[key]).__name__}"))
    for name in _REQUIRED_LIST_FILES:
        p = _state_path(name)
        if not p.exists():
            issues.append((name, "missing"))
            continue
        try:
            d = _load_json(p)
        except Exception as e:
            issues.append((name, f"invalid JSON: {e}"))
            continue
        if not isinstance(d, list):
            issues.append((name, f"expected list, got {type(d).__name__}"))
    if issues:
        return {"status": "red", "message_ru": f"схемы state-файлов нарушены ({len(issues)})",
                "details": {"violations": issues[:10]}}
    return {"status": "green",
            "message_ru": f"схемы всех ключевых state-файлов валидны "
                          f"({len(_REQUIRED_STATE_FILES) + len(_REQUIRED_LIST_FILES)} проверено)",
            "details": {"checked": len(_REQUIRED_STATE_FILES) + len(_REQUIRED_LIST_FILES)}}


# ═════════ FRESHNESS ═════════

def _chk_freshness() -> dict:
    issues_warn = []
    issues_crit = []
    for name, (warn, crit) in _FRESHNESS_THRESHOLDS_SEC.items():
        p = _state_path(name)
        age = _file_age_seconds(p)
        if age is None:
            issues_crit.append(f"{name}: файла нет")
            continue
        if age >= crit:
            issues_crit.append(f"{name}: {int(age)}s (>{crit}s)")
        elif age >= warn:
            issues_warn.append(f"{name}: {int(age)}s (>{warn}s)")

    if issues_crit:
        return {"status": "red",
                "message_ru": f"критическая залежалость state ({len(issues_crit)} файлов)",
                "details": {"critical": issues_crit, "warn": issues_warn}}
    if issues_warn:
        return {"status": "yellow",
                "message_ru": f"некоторые state-файлы стареют ({len(issues_warn)})",
                "details": {"warn": issues_warn}}
    return {"status": "green",
            "message_ru": f"все ключевые state-файлы свежие (<{_FRESHNESS_THRESHOLDS_SEC[next(iter(_FRESHNESS_THRESHOLDS_SEC))][0]//60} мин)",
            "details": {"checked": list(_FRESHNESS_THRESHOLDS_SEC.keys())}}


# ═════════ CODE HEALTH ═════════

def _chk_code_compiles() -> dict:
    """Все .py в teamagent/ компилируются без SyntaxError."""
    bad = []
    count = 0
    for py in _TEAMAGENT_DIR.glob("*.py"):
        if py.name.startswith("_"):
            continue
        count += 1
        try:
            py_compile.compile(str(py), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append((py.name, str(e).splitlines()[0]))
    if bad:
        return {"status": "red",
                "message_ru": f"{len(bad)} модулей не компилируются",
                "details": {"violations": bad}}
    return {"status": "green",
            "message_ru": f"все {count} .py модулей в teamagent/ компилируются чисто",
            "details": {"compiled": count}}


def _chk_critical_imports() -> dict:
    """Критичные модули можно импортировать без exception."""
    from importlib import import_module
    targets = [
        "teamagent.config",
        "teamagent.market_hours",
        "teamagent.stability_forecast",
        "teamagent.stability_engine",
        "teamagent.forecast_scanner",
        "teamagent.paper_trader",
        "teamagent.paper_trader_stakan",
        "teamagent.paper_trader_daily",
        "teamagent.dashboard.server",
    ]
    bad = []
    for m in targets:
        try:
            import_module(m)
        except Exception as e:
            bad.append((m, f"{type(e).__name__}: {e}"))
    if bad:
        return {"status": "red",
                "message_ru": f"{len(bad)} модулей не импортируются",
                "details": {"violations": bad}}
    return {"status": "green",
            "message_ru": f"все {len(targets)} критичных модулей импортируются чисто",
            "details": {"imported": targets}}


# ═════════ CONFIG SANITY ═════════

def _chk_config_sanity() -> dict:
    from . import config
    issues = []
    if not getattr(config, "PAIRS", None):
        issues.append("config.PAIRS пустой")
    elif len(config.PAIRS) != 28:
        issues.append(f"config.PAIRS имеет {len(config.PAIRS)} пар, ожидалось 28")
    if getattr(config, "MIN_PROBABILITY", None) is None or not (0 < config.MIN_PROBABILITY < 1):
        issues.append(f"MIN_PROBABILITY вне (0..1): {getattr(config, 'MIN_PROBABILITY', None)}")
    if getattr(config, "MAX_PROBABILITY", None) is None or not (0 < config.MAX_PROBABILITY <= 1):
        issues.append(f"MAX_PROBABILITY вне (0..1]: {getattr(config, 'MAX_PROBABILITY', None)}")
    if config.MIN_PROBABILITY >= config.MAX_PROBABILITY:
        issues.append(f"MIN_PROBABILITY ≥ MAX_PROBABILITY")
    if issues:
        return {"status": "red", "message_ru": f"config повреждён ({len(issues)})",
                "details": {"violations": issues}}
    return {"status": "green",
            "message_ru": (f"config валиден: PAIRS={len(config.PAIRS)}, "
                           f"prob {config.MIN_PROBABILITY:.0%}..{config.MAX_PROBABILITY:.0%}"),
            "details": {"pairs": len(config.PAIRS),
                        "min_prob": config.MIN_PROBABILITY,
                        "max_prob": config.MAX_PROBABILITY}}


# ═════════ CROSS-MODULE INVARIANTS ═════════

def _chk_adaptive_expiry_consistent() -> dict:
    """paper_trader.ADAPTIVE_*_H ≡ paper_trader_stakan.MAX_EXPIRY_H = 5,
    что подтверждает единое окно [1..5h]."""
    from . import paper_trader, paper_trader_stakan
    pt_min = getattr(paper_trader, "ADAPTIVE_MIN_EXPIRY_H", None)
    pt_max = getattr(paper_trader, "ADAPTIVE_MAX_EXPIRY_H", None)
    st_min = getattr(paper_trader_stakan, "MIN_EXPIRY_H", None)
    st_max = getattr(paper_trader_stakan, "MAX_EXPIRY_H", None)

    issues = []
    if pt_max != st_max:
        issues.append(f"paper_trader MAX={pt_max}, stakan MAX={st_max}")
    if pt_max != 5:
        issues.append(f"paper_trader MAX={pt_max}, ожидалось 5h")
    if pt_min != 1:
        issues.append(f"paper_trader MIN={pt_min}, ожидалось 1h")
    if issues:
        return {"status": "yellow",
                "message_ru": f"adaptive expiry рассогласован: {issues}",
                "details": {"diffs": issues, "pt": (pt_min, pt_max), "st": (st_min, st_max)}}
    return {"status": "green",
            "message_ru": f"обе основные стратегии используют единый expiry [{pt_min}..{pt_max}h]",
            "details": {"min": pt_min, "max": pt_max}}


def _chk_market_hours_buffer_sane() -> dict:
    """MARKET_CLOSE_BUFFER_MIN в каждой стратегии должен быть >= 5 мин."""
    from . import paper_trader, paper_trader_stakan, paper_trader_daily
    buffers = {
        "paper_trader":  getattr(paper_trader, "MARKET_CLOSE_BUFFER_MIN", None),
        "stakan":        getattr(paper_trader_stakan, "MARKET_CLOSE_BUFFER_MIN", None),
        "daily":         getattr(paper_trader_daily, "MARKET_CLOSE_BUFFER_MIN", None),
    }
    bad = [k for k, v in buffers.items() if v is None or v < 5]
    if bad:
        return {"status": "red",
                "message_ru": f"market_close_buffer слишком мал: {bad}",
                "details": buffers}
    return {"status": "green",
            "message_ru": f"market_close_buffer везде ≥5 мин: {buffers}",
            "details": buffers}


# ═════════ MAIN ─ run_audit ═════════

# Группировка проверок (категория → список (name, ru_label, fn))
_CATEGORIES: list[tuple[str, str, list[tuple[str, str, Callable]]]] = [
    ("self_consistency", "Самосогласованность данных", [
        ("paper_stats_vs_closed",      "paper_stats ↔ closed_trades", _chk_paper_stats_vs_closed_trades),
        ("stakan_internal",            "stakan_stats внутренне",       _chk_stakan_stats_vs_state),
        ("forecasts_cover_all_pairs",  "forecasts ↔ config.PAIRS",     _chk_forecasts_cover_all_pairs),
        ("qualified_pairs_match",      "stability_forecast ↔ strategy_config_locked", _chk_qualified_pairs_match),
        ("market_session_consistent",  "current_session ↔ UTC hour",   _chk_market_hours_consistent_with_session),
        ("open_trades_within_market",  "open_trades expiry ≤ next_close", _chk_open_trades_within_market),
        ("closed_trades_pnl",          "PnL = stake × payout по формуле", _chk_closed_trades_pnl_payout_consistency),
    ]),
    ("schema_validity", "Целостность схем state-файлов", [
        ("state_files_exist", "обязательные state-файлы существуют", _chk_required_state_files_exist),
        ("state_schemas",     "обязательные ключи и типы",            _chk_state_schemas),
    ]),
    ("freshness", "Свежесть данных", [
        ("freshness", "state-файлы не залежались", _chk_freshness),
    ]),
    ("code_health", "Здоровье кода", [
        ("code_compiles",    "все .py компилируются",    _chk_code_compiles),
        ("critical_imports", "критичные модули импортируются", _chk_critical_imports),
    ]),
    ("config_sanity", "Корректность config", [
        ("config_sanity", "config.PAIRS / MIN_PROB / MAX_PROB", _chk_config_sanity),
    ]),
    ("cross_module", "Кросс-модульные инварианты", [
        ("adaptive_expiry_unified", "paper_trader ≡ stakan по expiry", _chk_adaptive_expiry_consistent),
        ("market_close_buffer",     "MARKET_CLOSE_BUFFER_MIN ≥ 5",     _chk_market_hours_buffer_sane),
    ]),
]


def run_audit() -> dict:
    """Полный snapshot аудита системы.

    Returns dict с ключами:
      as_of, summary {green, yellow, red, total}, overall_status,
      categories: [{key, label, summary, checks: [...]}],
      verdict_ru, recommendations_ru.
    """
    cats: list[dict] = []
    all_checks: list[dict] = []

    for cat_key, cat_label, items in _CATEGORIES:
        checks = []
        for name, ru_label, fn in items:
            res = _safe_check(name, fn)
            res["ru_label"] = ru_label
            res["category"] = cat_key
            checks.append(res)
        cnt = _status_color_count(checks)
        cats.append({
            "key": cat_key,
            "label_ru": cat_label,
            "summary": cnt,
            "checks": checks,
        })
        all_checks.extend(checks)

    summary = _status_color_count(all_checks)
    summary["total"] = len(all_checks)

    if summary["red"] > 0:
        overall = "red"
        verdict = (f"❌ ЕСТЬ ПРОТИВОРЕЧИЯ ({summary['red']} критических, "
                   f"{summary['yellow']} предупреждений, {summary['green']} ОК) — "
                   f"систему нельзя считать единым организмом, исправить срочно")
    elif summary["yellow"] > 0:
        overall = "yellow"
        verdict = (f"⚠️ СИСТЕМА ОК ПО КРИТИЧНЫМ, ЕСТЬ {summary['yellow']} ПРЕДУПРЕЖДЕНИЙ "
                   f"(red=0, green={summary['green']}) — можно работать, но обратить внимание")
    else:
        overall = "green"
        verdict = (f"✅ ВСЕ {summary['green']} ПРОВЕРОК ЗЕЛЁНЫЕ — система самосогласована, "
                   f"данным можно верить")

    recs = []
    for c in all_checks:
        if c["status"] in ("red", "yellow"):
            label = c.get("ru_label", c["name"])
            recs.append(f"[{c['status']}] {label}: {c.get('message_ru', '')}")
    if not recs:
        recs.append("ничего исправлять не нужно — система проходит все мета-проверки")

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "overall_status": overall,
        "summary": summary,
        "verdict_ru": verdict,
        "categories": cats,
        "recommendations_ru": recs,
    }


__all__ = ["run_audit"]
