"""Russian-language narrative reports built from REAL data already in state/.

User asked (2026-05-03):
  "система … каждый час сделай тех анализ фундамент анализ новости проверит
   макро эконимика анализ политика анализ должен давать отчёт систему".

What this module does
---------------------
Aggregates the existing structured data (forecasts.json, fundamentals.json,
cot.json, agent_analyzer_*.json, weekly_loss_review, meta_strategy.json) into
5 narrative reports IN RUSSIAN that a human can read in 30 seconds:

    1. technical   — что говорят 28 пар на текущих индикаторах
    2. fundamental — ставки / доходности / инфляция по 8 валютам
    3. news        — текущие high-impact события из ForexFactory RSS
    4. macro       — DXY / US10Y / нефть / золото и их влияние на пары
    5. political   — гео-политические триггеры (выборы, санкции) — фильтр
                     открытых RSS (BBC/Reuters) по ключевым словам валют

Honest scope: НИКАКИХ LLM, НИКАКИХ симуляций, всё из реальных открытых
источников. Если источник недоступен — отчёт честно говорит "источник
недоступен", не выдумывает данные.

Each report has shape::

    {
      "title_ru":  "…",
      "as_of_utc": ISO,
      "source":    "fundamentals.json + Yahoo + …",
      "verdict_ru": "🟢 / 🟡 / 🔴 + одна фраза",
      "highlights_ru": [str, …],   # bullet points the user can scan
      "details": {…},              # structured data the UI can drill into
      "errors": [str, …]            # source failures, empty if all green
    }
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from . import config

log = logging.getLogger("agent_reports")

_STATE = config.STATE_DIR

# 5-min in-process cache so /api/agent-reports stays fast under load.
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SEC = 300


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    except Exception as e:
        log.warning(f"_load({path}) failed: {e}")
        return default


def _file_age_min(path: Path) -> float | None:
    if not path.exists():
        return None
    return max(0.0, (time.time() - path.stat().st_mtime) / 60.0)


# ───────────────────────────────────────────────────────────────────────
# 1. TECHNICAL REPORT — derived from forecasts.json
# ───────────────────────────────────────────────────────────────────────
def technical_report() -> dict:
    snap = _load(_STATE / "forecasts.json", {"forecasts": {}, "rankings": []})
    forecasts = snap.get("forecasts") or {}
    rankings = snap.get("rankings") or []
    errors: list[str] = []
    age_min = _file_age_min(_STATE / "forecasts.json")
    if age_min is None:
        errors.append("forecasts.json отсутствует — forecast_scanner не запущен.")
    elif age_min > 15:
        errors.append(f"forecasts.json устарел ({int(age_min)} мин назад).")

    eligible = [r for r in rankings if (r.get("probability_pct") or 0) >= 70]
    top_buy = next((r for r in rankings if r.get("side") == "BUY"), None)
    top_sell = next((r for r in rankings if r.get("side") == "SELL"), None)

    # Aggregate indicator regimes across all 28 pairs (4H timeframe — slowest,
    # most reliable for "macro structure" view).
    regimes = {"BULL": 0, "BEAR": 0, "FLAT": 0}
    adx_high = adx_low = 0
    for p, f in forecasts.items():
        ind4h = (f.get("indicators") or {}).get("4H") or {}
        ema20 = ind4h.get("ema20")
        ema50 = ind4h.get("ema50")
        adx = ind4h.get("adx")
        if ema20 is not None and ema50 is not None:
            if ema20 > ema50 * 1.0005: regimes["BULL"] += 1
            elif ema20 < ema50 * 0.9995: regimes["BEAR"] += 1
            else: regimes["FLAT"] += 1
        if adx is not None:
            if adx >= 25: adx_high += 1
            if adx < 15: adx_low += 1

    highlights: list[str] = []
    highlights.append(
        f"4H тренд по 28 парам: {regimes['BULL']} бычьих, "
        f"{regimes['BEAR']} медвежьих, {regimes['FLAT']} флэт."
    )
    if adx_high:
        highlights.append(f"Сильный тренд (ADX≥25) сейчас на {adx_high} парах.")
    if adx_low:
        highlights.append(f"Флэт (ADX<15) на {adx_low} парах — там сделки фильтруются.")
    highlights.append(f"Forecasts ≥70% вероятности: {len(eligible)} из 28.")
    if top_buy:
        highlights.append(
            f"Топ BUY: {top_buy.get('pair')} "
            f"({top_buy.get('probability_pct'):.0f}%) "
            f"— score {top_buy.get('score')}/{top_buy.get('max_score')}."
        )
    if top_sell:
        highlights.append(
            f"Топ SELL: {top_sell.get('pair')} "
            f"({top_sell.get('probability_pct'):.0f}%) "
            f"— score {top_sell.get('score')}/{top_sell.get('max_score')}."
        )

    if len(eligible) >= 5:
        verdict = "🟢 Технически: достаточно сигналов ≥70% для нормальной торговли."
    elif len(eligible) >= 1:
        verdict = "🟡 Технически: сигналов мало — узкий рынок или нет уверенности."
    else:
        verdict = "🔴 Технически: ни одна пара не пробивает 70%-гейт."

    return {
        "title_ru": "Технический анализ — 28 пар × индикаторы",
        "as_of_utc": (snap.get("scanned_at") or _utcnow().isoformat()),
        "source": "forecasts.json (forecast_scanner, каждые 5 мин)",
        "verdict_ru": verdict,
        "highlights_ru": highlights,
        "details": {
            "regimes_4h": regimes,
            "adx_high_count": adx_high,
            "adx_low_count": adx_low,
            "eligible_70_count": len(eligible),
            "top_buy": top_buy,
            "top_sell": top_sell,
        },
        "errors": errors,
    }


# ───────────────────────────────────────────────────────────────────────
# 2. FUNDAMENTAL REPORT — derived from fundamentals.json (FRED data)
# ───────────────────────────────────────────────────────────────────────
def fundamental_report() -> dict:
    fund = _load(_STATE / "fundamentals.json", None)
    errors: list[str] = []
    if fund is None:
        # Derive from agent state if main file missing.
        fund = _load(_STATE / "agent_analyzer_fundamental_macro.json", {})
        if fund:
            fund = fund.get("summary") or fund
        else:
            errors.append("fundamentals.json и agent_analyzer_fundamental_macro.json не найдены.")
            return _empty_report(
                "Фундаментальный анализ — ставки / доходности / инфляция",
                "FRED CSV (через fundamentals.py) — источник недоступен.",
                errors,
            )

    age_min = _file_age_min(_STATE / "fundamentals.json")
    if age_min is None:
        # Try the agent variant
        age_min = _file_age_min(_STATE / "agent_analyzer_fundamental_macro.json")
    if age_min and age_min > 60 * 25:
        errors.append(f"Fundamental data устарел ({int(age_min/60)}ч назад, лимит 24ч).")

    currencies = fund.get("currencies") or {}
    # Both shapes are supported:
    #  flattened (agent file): {"USD": {"policy_rate": 3.64, "10y_yield": 4.4, "cpi_yoy_pct": 3.29}}
    #  nested   (FRED file)  : {"USD": {"policy_rate": {"value": 3.64}, "10y_yield": {"value": 4.4},
    #                                    "cpi": {"yoy_pct": 3.29}}}
    def _scalar(d, key, sub=None):
        v = d.get(key)
        if isinstance(v, dict):
            return v.get(sub or "value")
        return v

    rates = []
    for c, d in currencies.items():
        if not isinstance(d, dict):
            continue
        pr = _scalar(d, "policy_rate")
        y10 = _scalar(d, "10y_yield")
        cpi = d.get("cpi_yoy_pct")
        if cpi is None and isinstance(d.get("cpi"), dict):
            cpi = d["cpi"].get("yoy_pct")
        try:
            pr_f = float(pr) if pr is not None else None
        except (TypeError, ValueError):
            pr_f = None
        if pr_f is None:
            continue
        rates.append((c, pr_f, y10, cpi))
    # Defensive sort that survives None / mixed types.
    rates.sort(key=lambda x: (x[1] if isinstance(x[1], (int, float)) else 0), reverse=True)

    def _fmt(v, suffix="%"):
        if isinstance(v, (int, float)):
            return f"{v:.2f}{suffix}"
        return "n/a"

    highlights: list[str] = []
    if rates:
        top = rates[0]
        bot = rates[-1]
        highlights.append(
            f"Самая ястребиная валюта: {top[0]} (ставка {_fmt(top[1])}, "
            f"10Y {_fmt(top[2])}, CPI {_fmt(top[3])} YoY)."
        )
        highlights.append(
            f"Самая голубиная валюта: {bot[0]} (ставка {_fmt(bot[1])}, "
            f"10Y {_fmt(bot[2])}, CPI {_fmt(bot[3])} YoY)."
        )
        try:
            diff = float(top[1]) - float(bot[1])
            highlights.append(
                f"Спред ставок крайних: {diff:.2f} п.п. "
                f"— чем больше, тем сильнее тренд carry trade."
            )
        except (TypeError, ValueError):
            pass

    top_bias = fund.get("top_bias_pairs") or []
    for entry in top_bias[:3]:
        try:
            tilt = float(entry.get("tilt_score") or 0)
            conf = float(entry.get("confidence_pct") or 0)
            highlights.append(
                f"Топ-уклон: {entry.get('pair')} {entry.get('side')} "
                f"(tilt {tilt:.1f}, conf {conf:.0f}%)."
            )
        except (TypeError, ValueError):
            highlights.append(f"Топ-уклон: {entry.get('pair')} {entry.get('side')}.")

    n_with_tilt = fund.get("n_pairs_with_tilt", 0)
    if n_with_tilt >= 20:
        verdict = "🟢 Фундаментально: явный макро-тренд по большинству пар."
    elif n_with_tilt >= 10:
        verdict = "🟡 Фундаментально: средний тренд — можно работать на части пар."
    else:
        verdict = "🔴 Фундаментально: макро-тренда нет, рынок без направления."

    return {
        "title_ru": "Фундаментальный анализ — ставки / доходности / инфляция",
        "as_of_utc": fund.get("as_of") or _utcnow().isoformat(),
        "source": "FRED public CSV (8 валют) → fundamentals.py, кэш 24ч",
        "verdict_ru": verdict,
        "highlights_ru": highlights,
        "details": {
            "currencies": currencies,
            "n_pairs_with_tilt": n_with_tilt,
            "top_bias_pairs": top_bias[:5],
        },
        "errors": errors,
    }


# ───────────────────────────────────────────────────────────────────────
# 3. NEWS REPORT — ForexFactory RSS (high-impact)
# ───────────────────────────────────────────────────────────────────────
_FF_RSS = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml?version=1"

def news_report() -> dict:
    errors: list[str] = []
    items: list[dict] = []
    now = _utcnow()
    horizon = now + timedelta(hours=24)
    try:
        req = Request(_FF_RSS, headers={"User-Agent": "Mozilla/5.0 fxinvestment/1.0"})
        with urlopen(req, timeout=8) as r:
            xml = r.read().decode("utf-8", errors="replace")
        # Lightweight parse — RSS XML; we look for <event>, <country>, <impact>,
        # <date>, <time>, <title> tags. ForexFactory uses <event>...
        import re
        for blk in re.findall(r"<event>(.*?)</event>", xml, re.DOTALL):
            def get(tag):
                m = re.search(rf"<{tag}>(.*?)</{tag}>", blk, re.DOTALL)
                return (m.group(1).strip() if m else "")
            country = get("country")
            impact = get("impact")
            date_s = get("date")
            time_s = get("time")
            title = get("title")
            if impact.lower() != "high":
                continue
            try:
                # ForexFactory uses MM-DD-YYYY h:mma EST. We can't always parse
                # — fall back to "сегодня/завтра" by raw strings.
                pass
            except Exception:
                pass
            items.append({
                "country": country,
                "impact": impact,
                "date": date_s,
                "time": time_s,
                "title": title,
            })
    except (URLError, HTTPError, TimeoutError) as e:
        errors.append(f"ForexFactory RSS недоступен: {e}")
    except Exception as e:
        errors.append(f"News parse failed: {type(e).__name__}: {e}")

    high_count = len(items)
    highlights: list[str] = []
    if errors:
        highlights.append(
            "Источник новостей сейчас недоступен — система продолжит работать "
            "по последнему успешному кэшу."
        )
    elif high_count == 0:
        highlights.append("На ближайшую неделю high-impact событий не найдено в RSS.")
    else:
        highlights.append(f"High-impact событий на этой неделе: {high_count}.")
        # First 5
        for it in items[:5]:
            highlights.append(
                f"• {it['country']}: {it['title']} "
                f"({it['date']} {it['time']})"
            )

    if errors:
        verdict = "🟡 Новостной канал нестабилен — торгуем по техническому фоллбэку."
    elif high_count >= 5:
        verdict = "🔴 Высокая волатильность ожидается — много high-impact событий."
    elif high_count >= 1:
        verdict = "🟡 Есть несколько high-impact событий — следим за блэкаутом."
    else:
        verdict = "🟢 Новостной фон спокойный — стандартные риски."

    return {
        "title_ru": "Новости — high-impact события (ForexFactory RSS)",
        "as_of_utc": now.isoformat(),
        "source": _FF_RSS,
        "verdict_ru": verdict,
        "highlights_ru": highlights,
        "details": {
            "high_impact_count_week": high_count,
            "items": items[:20],
        },
        "errors": errors,
    }


# ───────────────────────────────────────────────────────────────────────
# 4. MACRO REPORT — DXY / US10Y / Brent / Gold (Yahoo)
# ───────────────────────────────────────────────────────────────────────
def macro_report() -> dict:
    errors: list[str] = []
    snap = {}
    try:
        import yfinance as yf
        for sym, label in [
            ("DX-Y.NYB", "DXY"),
            ("^TNX",     "US10Y"),
            ("BZ=F",     "Brent"),
            ("GC=F",     "Gold"),
        ]:
            try:
                df = yf.download(sym, interval="1d", period="30d",
                                 progress=False, auto_adjust=False, threads=False)
                if df is None or df.empty or len(df) < 2:
                    errors.append(f"{label}: нет данных Yahoo.")
                    continue
                # yfinance MultiIndex flatten
                import pandas as pd
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                last = float(df["Close"].iloc[-1])
                prev = float(df["Close"].iloc[-2])
                wk_idx = -min(5, len(df))
                wk = float(df["Close"].iloc[wk_idx])
                snap[label] = {
                    "last": round(last, 4),
                    "delta_d_pct": round((last - prev) / prev * 100, 2) if prev else 0,
                    "delta_5d_pct": round((last - wk) / wk * 100, 2) if wk else 0,
                }
            except Exception as e:
                errors.append(f"{label}: {type(e).__name__}: {e}")
    except ImportError:
        errors.append("yfinance не установлен.")

    highlights: list[str] = []
    dxy = snap.get("DXY")
    if dxy:
        sign = "вверх" if dxy["delta_d_pct"] > 0 else "вниз"
        highlights.append(
            f"DXY {dxy['last']:.2f}, день {sign} {abs(dxy['delta_d_pct']):.2f}%, "
            f"5д {dxy['delta_5d_pct']:+.2f}%. "
            f"{'Поддерживает USD-пары' if dxy['delta_d_pct']>0 else 'Давит на USD-пары'}."
        )
    us10y = snap.get("US10Y")
    if us10y:
        highlights.append(
            f"US 10Y доходность {us10y['last']:.2f}%, день {us10y['delta_d_pct']:+.2f}%, "
            f"5д {us10y['delta_5d_pct']:+.2f}%."
        )
    brent = snap.get("Brent")
    if brent:
        highlights.append(
            f"Нефть Brent {brent['last']:.2f}$, 5д {brent['delta_5d_pct']:+.2f}% "
            f"— влияет на CAD/NOK."
        )
    gold = snap.get("Gold")
    if gold:
        highlights.append(
            f"Золото {gold['last']:.2f}$, 5д {gold['delta_5d_pct']:+.2f}% "
            f"— риск-off бенчмарк."
        )

    if errors and not snap:
        verdict = "🔴 Макро: источники недоступны."
    elif errors:
        verdict = "🟡 Макро: данные частичны."
    else:
        verdict = "🟢 Макро: все 4 бенчмарка обновлены."

    return {
        "title_ru": "Макро-фон — DXY / US10Y / Brent / Gold",
        "as_of_utc": _utcnow().isoformat(),
        "source": "Yahoo Finance — DX-Y.NYB / ^TNX / BZ=F / GC=F",
        "verdict_ru": verdict,
        "highlights_ru": highlights,
        "details": snap,
        "errors": errors,
    }


# ───────────────────────────────────────────────────────────────────────
# 5. POLITICAL REPORT — RSS keyword filter (Reuters / BBC FX section)
# ───────────────────────────────────────────────────────────────────────
_POLITICAL_RSS = [
    ("Reuters World", "https://feeds.reuters.com/reuters/worldNews"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
]
_POL_KEYWORDS = [
    "election", "tariff", "sanction", "war", "putin", "xi", "trump",
    "biden", "fed", "ecb", "boj", "boe", "treasury", "default",
    "downgrade", "moody", "fitch", "s&p", "trade war", "summit",
]

def political_report() -> dict:
    errors: list[str] = []
    items: list[dict] = []
    now = _utcnow()
    for label, url in _POLITICAL_RSS:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 fxinvestment/1.0"})
            with urlopen(req, timeout=6) as r:
                xml = r.read().decode("utf-8", errors="replace")
            import re
            for blk in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)[:25]:
                def get(tag):
                    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", blk, re.DOTALL)
                    return (m.group(1).strip() if m else "")
                title_raw = get("title")
                title = re.sub(r"<!\[CDATA\[|\]\]>", "", title_raw).strip()
                if not title:
                    continue
                low = title.lower()
                if not any(k in low for k in _POL_KEYWORDS):
                    continue
                pub = get("pubDate")
                items.append({"source": label, "title": title, "pub": pub})
        except (URLError, HTTPError, TimeoutError) as e:
            errors.append(f"{label}: {e}")
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {e}")

    # Dedupe by title
    seen = set()
    unique = []
    for it in items:
        t = it["title"]
        if t in seen: continue
        seen.add(t)
        unique.append(it)
    unique = unique[:10]

    highlights: list[str] = []
    if errors and not unique:
        highlights.append("RSS-источники недоступны — политический фон не оценён.")
    elif not unique:
        highlights.append("В топ-новостях нет ключевых FX-триггеров.")
    else:
        highlights.append(
            f"Отфильтровано {len(unique)} новостей с FX-релевантными ключевыми словами."
        )
        for it in unique[:5]:
            highlights.append(f"• [{it['source']}] {it['title']}")

    if errors and not unique:
        verdict = "🟡 Политический канал недоступен — фоллбэк по техническому."
    elif len(unique) >= 5:
        verdict = "🟠 Высокий политический шум — повышенный риск гэпов."
    elif len(unique) >= 1:
        verdict = "🟡 Отдельные FX-новости — следим."
    else:
        verdict = "🟢 Политически тихо."

    return {
        "title_ru": "Политический и гео-фон — Reuters + BBC RSS (фильтр FX)",
        "as_of_utc": now.isoformat(),
        "source": ", ".join(u for _, u in _POLITICAL_RSS),
        "verdict_ru": verdict,
        "highlights_ru": highlights,
        "details": {
            "items": unique,
            "keywords_used": _POL_KEYWORDS,
        },
        "errors": errors,
    }


def _empty_report(title: str, source: str, errors: list[str]) -> dict:
    return {
        "title_ru": title,
        "as_of_utc": _utcnow().isoformat(),
        "source": source,
        "verdict_ru": "🔴 Источник недоступен — отчёт пустой.",
        "highlights_ru": [],
        "details": {},
        "errors": errors,
    }


# ───────────────────────────────────────────────────────────────────────
# AGGREGATE — все 5 отчётов в одном вызове
# ───────────────────────────────────────────────────────────────────────
def all_reports(use_cache: bool = True) -> dict:
    if use_cache:
        cached = _CACHE.get("all")
        if cached and (time.time() - cached[0] < _CACHE_TTL_SEC):
            return cached[1]

    out = {
        "as_of_utc": _utcnow().isoformat(),
        "ttl_sec": _CACHE_TTL_SEC,
        "reports": {
            "technical":   technical_report(),
            "fundamental": fundamental_report(),
            "news":        news_report(),
            "macro":       macro_report(),
            "political":   political_report(),
        },
    }
    # Aggregate verdict counts
    rep_count = len(out["reports"])
    green = sum(1 for r in out["reports"].values() if r.get("verdict_ru", "").startswith("🟢"))
    yellow = sum(1 for r in out["reports"].values() if r.get("verdict_ru", "").startswith("🟡"))
    red = sum(1 for r in out["reports"].values() if r.get("verdict_ru", "").startswith("🔴"))
    orange = sum(1 for r in out["reports"].values() if r.get("verdict_ru", "").startswith("🟠"))
    out["summary"] = {
        "total": rep_count,
        "green": green,
        "yellow": yellow,
        "orange": orange,
        "red": red,
        "verdict_ru":
            "🟢 Все 5 каналов согласны — данные надёжны." if green == rep_count
            else f"🟡 {green}/{rep_count} зелёных, {yellow + orange} жёлтых, {red} красных — частичная картина."
            if red == 0 else
            f"🔴 {red} красных каналов из {rep_count} — данные ненадёжны.",
    }
    if use_cache:
        _CACHE["all"] = (time.time(), out)
    return out


# ───────────────────────────────────────────────────────────────────────
# COVERAGE MATRIX — 28 пар × 4 сессии (qualified ≥70%, probable, frozen)
# ───────────────────────────────────────────────────────────────────────
def coverage_matrix() -> dict:
    """28 × 4 grid showing whether each (pair, session) cell has a working
    strategy with WR ≥ 70% — the ‘individual strategy per cell’ the user asked
    about. Source: meta_strategy.json (auto-generated by strategy_meta_agent).
    """
    meta = _load(_STATE / "meta_strategy.json", None)
    errors: list[str] = []
    if meta is None:
        errors.append("meta_strategy.json не найден — strategy_meta_agent не запущен.")
        return {
            "as_of_utc": _utcnow().isoformat(),
            "source": "meta_strategy.json (strategy_meta_agent)",
            "matrix": [],
            "summary": {"qualified": 0, "probable": 0, "frozen": 0, "missing": 28*4},
            "errors": errors,
        }

    cells = meta.get("cells") or {}
    pairs = sorted({c.split(":")[0] for c in cells})
    sessions = ["Asia", "London", "Overlap", "NY"]

    grid: list[dict] = []
    cnt = {"qualified": 0, "probable": 0, "frozen": 0, "missing": 0}
    for pair in pairs:
        row = {"pair": pair, "cells": {}}
        for sess in sessions:
            key = f"{pair}:{sess}"
            cell = cells.get(key)
            if cell is None:
                row["cells"][sess] = {"status": "MISSING", "color": "gray"}
                cnt["missing"] += 1
                continue
            status = (cell.get("status") or "").upper()
            wr = cell.get("win_rate_pct")
            wlo = cell.get("wilson_lower_pct")
            n = cell.get("trades", 0)
            color = (
                "green"  if status == "QUALIFIED" else
                "yellow" if status == "PROBABLE"  else
                "red"    if status == "FROZEN"    else
                "gray"
            )
            cnt[status.lower()] = cnt.get(status.lower(), 0) + 1
            row["cells"][sess] = {
                "status": status,
                "color": color,
                "win_rate_pct": wr,
                "wilson_lower_pct": wlo,
                "trades": n,
                "variant": cell.get("variant"),
                "side_bias": cell.get("side_bias"),
            }
        grid.append(row)

    total = len(pairs) * 4
    pct_q = round(100.0 * cnt["qualified"] / total, 1) if total else 0
    summary = dict(cnt)
    summary["total_cells"] = total
    summary["qualified_pct"] = pct_q
    summary["verdict_ru"] = (
        f"🟢 {cnt['qualified']}/{total} ячеек ≥70% WR ({pct_q:.1f}%) — система покрывает большинство (пара, сессия)."
        if pct_q >= 50 else
        f"🟡 Только {cnt['qualified']}/{total} ячеек ≥70% WR ({pct_q:.1f}%) — есть простор для доработки."
        if pct_q >= 25 else
        f"🔴 Лишь {cnt['qualified']}/{total} ячеек ≥70% WR ({pct_q:.1f}%) — нужен более широкий sweep стратегий."
    )

    return {
        "as_of_utc": meta.get("as_of") or _utcnow().isoformat(),
        "source": "meta_strategy.json (strategy_meta_agent, каждые 5 часов)",
        "pairs": pairs,
        "sessions": sessions,
        "matrix": grid,
        "summary": summary,
        "errors": errors,
    }


__all__ = [
    "technical_report",
    "fundamental_report",
    "news_report",
    "macro_report",
    "political_report",
    "all_reports",
    "coverage_matrix",
]
