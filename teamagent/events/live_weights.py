"""Live event-weighting for forecast_scanner / paper_trader.

Loads the Phase-1..6 attribution artefacts once at import and exposes:

- persistent_event_in_window(pair, session, now_utc) → list of dicts
  describing persistent-driver events in ±6h of `now_utc` that affect `pair`.
- trap_risk(pair, session) → 0.0..1.0 (how often a significant move on this
  cell is a trap, derived from `trap_pair_session_summary.csv`).
- event_score_contribution(pair, session, now_utc) → (delta_score, reason)
  ready to feed into forecast_scanner.vote().

If the artefacts don't exist (e.g. on a fresh checkout before phase-6 was
run), all functions degrade to no-op safely.
"""
from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger("events.live_weights")

ROOT = Path(__file__).resolve().parents[2]
ART_DIR = ROOT / "HISTORY" / "event_attribution_365d"
EVENTS_FILE = ROOT / "teamagent" / "state" / "events_365d.json"
PERSISTENT_FILE = ART_DIR / "persistent_drivers.csv"
PER_EVENT_PS_FILE = ART_DIR / "per_event_pair_session.csv"
TRAP_PS_FILE = ART_DIR / "trap_pair_session_summary.csv"
LEARNED_RULES_FILE = ROOT / "teamagent" / "state" / "learned_rules.json"
STRATEGY_LOCKED_FILE = ROOT / "teamagent" / "state" / "strategy_config_locked.json"

WINDOW_HOURS = 6  # ±6h around current time

# Lazy-loaded caches
_loaded = False
_events: list[dict] = []
_events_by_date: dict[str, list[dict]] = defaultdict(list)
_persistent_types: dict[str, dict] = {}  # event_type → {persistence, concordance, total_matches}
_trap_risk: dict[tuple[str, str], float] = {}  # (pair, session) → trap_pct_of_significant
# (pair, session, event_type) → {dominant_direction, concordance, persistence, frequency}
_per_event_ps: dict[tuple[str, str, str], dict] = {}
# Phase-8 trained knowledge
# (pair, session, event_type) → high-conviction rule dict (learned_rules.json layer 1)
_high_conv_rules: dict[tuple[str, str, str], dict] = {}
# (pair, session) → {mean_signed_pips, concordance_pct, dominant_direction, n}
_pair_bias: dict[tuple[str, str], dict] = {}
# Phase-9 trained knowledge
# (pair, hour) → {n, ups, downs, concordance_pct, dominant_direction, mean_signed_pips}
_pair_hour_bias: dict[tuple[str, int], dict] = {}
# (pair, session) → {win_rate_pct, trades, dominant_side ('BUY'|'SELL')}
_strategy_wr: dict[tuple[str, str], dict] = {}


def _safe_float(x: str | None) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _load() -> None:
    """One-time load of all artefacts. Idempotent."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    # Events
    try:
        if EVENTS_FILE.exists():
            arch = json.loads(EVENTS_FILE.read_text())
            for e in arch.get("events", []):
                e["_ts"] = datetime.fromisoformat(e["ts"])
                if e["_ts"].tzinfo is None:
                    e["_ts"] = e["_ts"].replace(tzinfo=timezone.utc)
                _events.append(e)
                _events_by_date[e["_ts"].date().isoformat()].append(e)
            log.info(f"loaded {len(_events)} events from archive")
        else:
            log.warning(f"events archive not found at {EVENTS_FILE}; live-weights disabled")
    except Exception as e:
        log.warning(f"failed to load events archive: {e}")

    # Persistent drivers
    try:
        if PERSISTENT_FILE.exists():
            with open(PERSISTENT_FILE) as f:
                for row in csv.DictReader(f):
                    _persistent_types[row["event_type"]] = {
                        "persistence_24h_avg_pct": _safe_float(row.get("persistence_24h_avg_pct")) or 0.0,
                        "direction_concordance_pct": _safe_float(row.get("direction_concordance_pct")) or 0.0,
                        "total_matches": int(row.get("total_matches", 0)),
                        "abs_mean_move_pips": _safe_float(row.get("abs_mean_move_pips")) or 0.0,
                    }
            log.info(f"loaded {len(_persistent_types)} persistent event-types")
        else:
            log.info(f"persistent_drivers.csv not found; live-weights disabled")
    except Exception as e:
        log.warning(f"failed to load persistent_drivers: {e}")

    # Trap risk
    try:
        if TRAP_PS_FILE.exists():
            with open(TRAP_PS_FILE) as f:
                for row in csv.DictReader(f):
                    pair = row["pair"]
                    sess = row["session"]
                    rate = _safe_float(row.get("trap_pct_of_significant")) or 0.0
                    _trap_risk[(pair, sess)] = rate
            log.info(f"loaded {len(_trap_risk)} pair-session trap rates")
    except Exception as e:
        log.warning(f"failed to load trap_pair_session_summary: {e}")

    # per-pair-session-event direction lookup
    try:
        if PER_EVENT_PS_FILE.exists():
            with open(PER_EVENT_PS_FILE) as f:
                for row in csv.DictReader(f):
                    key = (row["pair"], row["session"], row["event_type"])
                    _per_event_ps[key] = {
                        "dominant_direction_event_ccy": row.get("dominant_direction_event_ccy", "flat"),
                        "concordance": _safe_float(row.get("direction_concordance_pct")) or 0.0,
                        "persistence_24h_pct": _safe_float(row.get("persistence_24h_avg_pct")) or 0.0,
                        "frequency": int(row.get("frequency", 0)),
                        "trap_rate_pct": _safe_float(row.get("trap_rate_pct")) or 0.0,
                    }
            log.info(f"loaded {len(_per_event_ps)} per-event-pair-session entries")
    except Exception as e:
        log.warning(f"failed to load per_event_pair_session: {e}")

    # Phase-8 learned rules + Phase-9 hour bias
    try:
        if LEARNED_RULES_FILE.exists():
            data = json.loads(LEARNED_RULES_FILE.read_text())
            for r in data.get("high_conviction_rules", []):
                _high_conv_rules[(r["pair"], r["session"], r["event_type"])] = r
            for pair, sess_map in data.get("pair_session_bias", {}).items():
                for sess, info in sess_map.items():
                    _pair_bias[(pair, sess)] = info
            for pair, hour_map in data.get("pair_hour_bias", {}).items():
                for hour_str, info in hour_map.items():
                    try:
                        hr = int(hour_str)
                    except (TypeError, ValueError):
                        continue
                    _pair_hour_bias[(pair, hr)] = info
            log.info(
                f"loaded {len(_high_conv_rules)} high-conviction rules + "
                f"{len(_pair_bias)} pair-session bias cells + "
                f"{len(_pair_hour_bias)} pair-hour bias cells"
            )
        else:
            log.info("learned_rules.json not found; phase-8/9 boost disabled")
    except Exception as e:
        log.warning(f"failed to load learned_rules: {e}")

    # Phase-9: strategy_config_locked → per-(pair, session) historical WR + dominant side
    try:
        if STRATEGY_LOCKED_FILE.exists():
            data = json.loads(STRATEGY_LOCKED_FILE.read_text())
            for pair, info in data.get("pairs", {}).items():
                by_sess = info.get("by_session") or {}
                for sess_name, sess_info in by_sess.items():
                    wr = sess_info.get("win_rate_pct")
                    trades = sess_info.get("trades") or 0
                    top_variants = sess_info.get("top_variants") or []
                    dom_side = None
                    if top_variants:
                        dom_side = top_variants[0].get("dominant_side")
                    if wr is None or dom_side not in ("BUY", "SELL"):
                        continue
                    _strategy_wr[(pair, sess_name)] = {
                        "win_rate_pct": float(wr),
                        "trades": int(trades),
                        "dominant_side": dom_side,
                    }
            log.info(f"loaded {len(_strategy_wr)} per-(pair, session) historical WR cells")
        else:
            log.info("strategy_config_locked.json not found; historical_wr boost disabled")
    except Exception as e:
        log.warning(f"failed to load strategy_config_locked: {e}")


def event_affects_pair(event_ccy: str, pair: str) -> bool:
    return pair[:3] == event_ccy or pair[3:6] == event_ccy


# Session-name compatibility: the analysis pipeline uses
# {Asia, London, Overlap, NY} (detector.py SESSIONS), but the runtime
# `forecast_scanner._current_session` uses {Asia, London, LON+NY, NY, Off}
# from `config.SESSIONS`. Map runtime → analysis name so the lookups hit.
_SESSION_ALIAS = {
    "LON+NY": "Overlap",
    "Asia": "Asia",
    "London": "London",
    "NY": "NY",
    "Overlap": "Overlap",  # already-normalised
}


def _hour_to_analysis_session(hour: int) -> str:
    """Map any UTC hour to the analysis-session name from detector.py.

    Detector windows: Asia (0-6), London (7-12), Overlap (13-16), NY (17-21).
    Hours 22-23 are not covered by any session in detector — we map them to
    "NY" because behaviour at 22-23 UTC is still tail-end of NY trading.
    """
    if 0 <= hour <= 6:
        return "Asia"
    if 7 <= hour <= 12:
        return "London"
    if 13 <= hour <= 16:
        return "Overlap"
    return "NY"  # 17-23


def _norm_session(s: str, hour: int | None = None) -> str:
    """Normalise session label from runtime → analysis taxonomy.

    If `s` is "Off" (or any unknown), fall back to hour-based mapping if
    `hour` was provided, otherwise return "Off" so the lookup misses.
    """
    if s in _SESSION_ALIAS:
        return _SESSION_ALIAS[s]
    if hour is not None:
        return _hour_to_analysis_session(hour)
    return s


def persistent_events_in_window(pair: str, now_utc: datetime, window_hours: int = WINDOW_HOURS) -> list[dict]:
    """Find persistent-driver events within ±window_hours that affect pair."""
    _load()
    if not _events or not _persistent_types:
        return []
    win_start = now_utc - timedelta(hours=window_hours)
    win_end = now_utc + timedelta(hours=window_hours)
    candidate_dates = {
        (now_utc - timedelta(days=1)).date().isoformat(),
        now_utc.date().isoformat(),
        (now_utc + timedelta(days=1)).date().isoformat(),
    }
    out: list[dict] = []
    for d in candidate_dates:
        for e in _events_by_date.get(d, []):
            if not (win_start <= e["_ts"] <= win_end and event_affects_pair(e["currency"], pair)):
                continue
            etype = e["type"]
            if etype not in _persistent_types:
                continue
            out.append(e)
    return out


def trap_risk(pair: str, session: str) -> float:
    """Return trap-rate (0..100) for pair × session. 0 if unknown."""
    _load()
    return _trap_risk.get((pair, _norm_session(session)), 0.0)


def event_score_contribution(
    pair: str,
    session: str,
    now_utc: datetime,
    base_score_pts: int = 4,
    window_hours: int = WINDOW_HOURS,
) -> tuple[int, str | None]:
    """Compute event-weight contribution for forecast_scanner.

    Logic:
    - For each persistent event in window:
      - look up (pair, session, event_type) entry to find dominant_direction
        for the event-currency; convert to pair-direction (BUY if event_ccy is
        BASE and direction='up'; opposite for QUOTE).
      - contribution magnitude = base_score_pts × concordance × persistence
        (both 0..1), then sign by direction.
    - Sum contributions; trim to ±2*base_score_pts to avoid overweighting.

    Returns (delta_score_int, reason_string) or (0, None) if no events.
    """
    _load()
    session = _norm_session(session, hour=now_utc.hour)
    events = persistent_events_in_window(pair, now_utc, window_hours)
    if not events:
        return 0, None

    total = 0.0
    parts: list[str] = []
    for e in events:
        etype = e["type"]
        meta = _per_event_ps.get((pair, session, etype))
        if not meta or meta["frequency"] < 2:
            continue
        conc = meta["concordance"] / 100.0
        persist = meta["persistence_24h_pct"] / 100.0
        # weight 0..base_score_pts
        weight = base_score_pts * conc * persist
        if weight < 0.5:
            continue
        # Direction: dominant for event-ccy. If event_ccy == base of pair,
        # 'up' means base ↑ → BUY pair. If event_ccy == quote of pair,
        # 'up' means quote ↑ → SELL pair (because quote up = pair down).
        dom = meta["dominant_direction_event_ccy"]
        if dom == "flat":
            continue
        is_base = pair[:3] == e["currency"]
        if dom == "up":
            sign = +1 if is_base else -1
        else:
            sign = -1 if is_base else +1
        contrib = round(weight * sign)
        if contrib == 0:
            continue
        total += contrib
        parts.append(f"{etype}({sign:+d}×{round(weight,1)})")
    # Trim to ±2*base_score_pts (don't let one news cluster overwhelm)
    cap = 2 * base_score_pts
    total = max(-cap, min(cap, total))
    if total == 0:
        return 0, None
    return int(round(total)), "event_attribution: " + ", ".join(parts)


def _direction_to_pair_sign(direction: str, event_ccy: str, pair: str) -> int:
    """Convert event-currency direction to pair-direction sign.

    If event_ccy is the BASE of the pair: 'up' means pair ↑ → +1 (BUY).
    If event_ccy is the QUOTE: 'up' means quote ↑ → pair ↓ → -1 (SELL).
    Returns 0 if direction is 'flat' or unknown.
    """
    if direction not in ("up", "down"):
        return 0
    is_base = pair[:3] == event_ccy
    if direction == "up":
        return +1 if is_base else -1
    return -1 if is_base else +1


def learned_rule_score(
    pair: str,
    session: str,
    now_utc: datetime,
    base_pts: int = 8,
    window_hours: int = WINDOW_HOURS,
) -> tuple[int, str | None]:
    """Phase-8 layer 1: high-conviction event rule boost.

    When a learned high-conviction rule (`learned_rules.json`) matches the
    current (pair × session × event_type) AND the event is in window, apply
    a strong score boost (up to ±base_pts × concordance × min(persistence,
    1)). Multiple matching rules sum, capped at ±2*base_pts.

    Compared to `event_score_contribution` (which uses ALL matched events),
    this only fires for the 17 high-conviction cells. The boost is bigger
    (default base=8 vs 4) because confidence is much higher.
    """
    _load()
    session = _norm_session(session, hour=now_utc.hour)
    if not _high_conv_rules or not _events:
        return 0, None
    win_start = now_utc - timedelta(hours=window_hours)
    win_end = now_utc + timedelta(hours=window_hours)
    candidate_dates = {
        (now_utc - timedelta(days=1)).date().isoformat(),
        now_utc.date().isoformat(),
        (now_utc + timedelta(days=1)).date().isoformat(),
    }
    total = 0.0
    parts: list[str] = []
    for d in candidate_dates:
        for e in _events_by_date.get(d, []):
            if not (win_start <= e["_ts"] <= win_end and event_affects_pair(e["currency"], pair)):
                continue
            rule = _high_conv_rules.get((pair, session, e["type"]))
            if not rule:
                continue
            sign = _direction_to_pair_sign(rule["dominant_direction_event_ccy"], e["currency"], pair)
            if sign == 0:
                continue
            conc = rule["concordance_pct"] / 100.0
            persist = min(rule["persistence_24h_pct"] / 100.0, 1.0)
            # weight = base * concordance * sqrt(min(freq,8)/4) so freq amplifies
            freq_term = min(rule["frequency"], 8) / 8.0
            weight = base_pts * conc * (0.6 + 0.4 * persist) * (0.7 + 0.3 * freq_term)
            contrib = round(weight * sign)
            if contrib == 0:
                continue
            total += contrib
            parts.append(f"learned[{e['type']}] {sign:+d}×{round(weight,1)}(conc{int(rule['concordance_pct'])})")
    cap = 2 * base_pts
    total = max(-cap, min(cap, total))
    if total == 0:
        return 0, None
    return int(round(total)), "learned_rule: " + ", ".join(parts)


def pair_session_bias_score(pair: str, session: str, base_pts: int = 3) -> tuple[int, str | None]:
    """Phase-8 layer 2 (relaxed in Phase 9): persistent pair-session directional bias.

    Even with no specific event in window, a (pair × session) cell may have a
    long-term drift. We translate this into a small constant nudge (max
    ±base_pts) when historical concordance ≥ 65% over ≥ 80 days.

    Phase 9 (2026-05-04) lowered the threshold from (conc≥70%, n≥100, cap=±2)
    to (conc≥65%, n≥80, cap=±3). 65% concordance over n≥80 days is still
    statistically significant (binomial p<0.01 vs fair coin). The wider net
    fires on more cells (~9 instead of ~5 today) so technical signals get an
    extra honest tilt aligned with the year-long drift.
    """
    _load()
    info = _pair_bias.get((pair, _norm_session(session)))
    if not info:
        return 0, None
    conc = info.get("concordance_pct", 0.0)
    n = info.get("n", 0)
    if conc < 65.0 or n < 80:
        return 0, None
    direction = info.get("dominant_direction", "flat")
    if direction not in ("up", "down"):
        return 0, None
    sign = +1 if direction == "up" else -1
    # Magnitude scales with concordance excess over 65%: 65%→1pt, 75%→2pts, 85%+→3pts.
    magnitude = max(1, min(base_pts, int(round((conc - 65.0) / 10.0)) + 1))
    delta = magnitude * sign
    mean_pips = info.get("mean_signed_pips", 0.0)
    return delta, f"pair_session_bias: {direction}×{magnitude} (conc={int(conc)}%, mean={mean_pips:+.1f}pips, n={n})"


def _pair_dir_to_pair_sign(direction: str) -> int:
    """Convert pair-level direction string ('up'/'down') to pair-sign (+1/-1)."""
    if direction == "up":
        return +1
    if direction == "down":
        return -1
    return 0


def hour_bias_score(pair: str, now_utc: datetime, base_pts: int = 1) -> tuple[int, str | None]:
    """Phase-9 layer 2b: per-(pair × UTC hour) directional drift.

    Reads `pair_hour_bias` artefact (built from 365-day Yahoo 1H closes by
    `training._build_pair_hour_bias`). When the current UTC hour for `pair`
    has historical concordance ≥ 62% over ≥ 60 days, add a small ±1 nudge
    in the dominant direction. Smaller cap than session-bias because hourly
    samples are noisier.
    """
    _load()
    if not _pair_hour_bias:
        return 0, None
    info = _pair_hour_bias.get((pair, now_utc.hour))
    if not info:
        return 0, None
    conc = info.get("concordance_pct", 0.0)
    n = info.get("n", 0)
    if conc < 62.0 or n < 60:
        return 0, None
    sign = _pair_dir_to_pair_sign(info.get("dominant_direction", "flat"))
    if sign == 0:
        return 0, None
    delta = base_pts * sign
    mean_pips = info.get("mean_signed_pips", 0.0)
    return delta, (
        f"hour_bias: hr={now_utc.hour:02d}UTC×{sign:+d}×{base_pts} "
        f"(conc={int(conc)}%, mean={mean_pips:+.2f}pips, n={n})"
    )


def historical_wr_score(
    pair: str,
    session: str,
    pre_score: int,
    base_pts: int = 4,
) -> tuple[int, str | None]:
    """Phase-9 layer 4: per-(pair × session) historical backtest WR vote.

    Reads `state/strategy_config_locked.json` (output of strategy_search) for
    the current (pair × session). If the cell's historical WR ≥ 60% on a
    statistically meaningful sample (≥ 8 trades), AND the cell's dominant
    historical side AGREES with the current technical-stack score sign,
    amplify in that direction. Magnitude tiers:
      WR ≥ 70% → ±4 (strong, qualified cell)
      WR 65-70% → ±3
      WR 60-65% → ±2
      WR < 60% → 0 (no edge to amplify)

    The agreement guard matters: strategy_search variants are usually one-
    sided filters (BUY-only or SELL-only) so a cell's `dominant_side` only
    reflects the side the best filter measured. We never use it to override
    technicals — only to amplify when both agree.
    """
    _load()
    info = _strategy_wr.get((pair, _norm_session(session)))
    if not info:
        return 0, None
    wr = info["win_rate_pct"]
    trades = info["trades"]
    if trades < 8 or wr < 60.0:
        return 0, None
    side = info["dominant_side"]
    sign = +1 if side == "BUY" else -1
    # Only amplify if technicals agree (same sign). Pre-score==0 means no
    # technical signal yet → don't impose direction.
    if pre_score == 0 or (pre_score > 0) != (sign > 0):
        return 0, None
    # Magnitude: 60-65 → 2, 65-70 → 3, 70-100 → 4
    if wr >= 70.0:
        magnitude = base_pts
    elif wr >= 65.0:
        magnitude = max(1, base_pts - 1)
    else:
        magnitude = max(1, base_pts - 2)
    delta = magnitude * sign
    return delta, (
        f"historical_wr: WR={wr:.1f}% × {side} × {magnitude} "
        f"(trades={trades}, agrees with technicals)"
    )


# Module-level cache for currency strength so we recompute at most once per
# 5 min — the same TTL used elsewhere for live data.
_CCY_STRENGTH_CACHE: dict[str, tuple[float, dict]] = {}
_CCY_STRENGTH_TTL_SEC = 300.0


def _compute_currency_strength_24h() -> dict[str, float]:
    """Compute 24h relative strength for each of the 8 majors.

    Uses real Yahoo 1H bars (last 24 closes) — no simulator. Each currency's
    strength = mean of its returns vs USD across the relevant pairs in our
    28-pair universe. USD's strength = inverted mean of all USD-quote and
    USD-base pairs.

    Returns {ccy: strength_value} where positive = currency strengthened
    over last 24h relative to USD.
    """
    import time as _time
    now_t = _time.time()
    cached = _CCY_STRENGTH_CACHE.get("data")
    if cached and now_t - cached[0] < _CCY_STRENGTH_TTL_SEC:
        return cached[1]

    try:
        from teamagent.data import yahoo as _yahoo
        from teamagent import config as _cfg
    except Exception as e:
        log.warning(f"currency_strength: import failed ({e})")
        return {}

    # Pair-level 24h return = (close[-1] - close[-25]) / close[-25]
    pair_returns: dict[str, float] = {}
    for pair in _cfg.PAIRS:
        try:
            df = _yahoo.latest_bars(pair, "1h", 30)
        except Exception:
            continue
        if df is None or len(df) < 25:
            continue
        close_col = "Close" if "Close" in df.columns else "close"
        try:
            c0 = float(df[close_col].iloc[-25])
            c1 = float(df[close_col].iloc[-1])
        except Exception:
            continue
        if c0 <= 0:
            continue
        pair_returns[pair] = (c1 - c0) / c0

    if not pair_returns:
        return {}

    # Per-currency: average return when currency is BASE; subtract average
    # return when currency is QUOTE. Effectively: how much it strengthened
    # vs the rest of the basket.
    accum: dict[str, list[float]] = defaultdict(list)
    for pair, ret in pair_returns.items():
        base = pair[:3]
        quote = pair[3:6]
        accum[base].append(ret)
        accum[quote].append(-ret)
    out = {ccy: (sum(v) / len(v)) for ccy, v in accum.items() if v}
    _CCY_STRENGTH_CACHE["data"] = (now_t, out)
    return out


def currency_strength_score(pair: str, base_pts: int = 2) -> tuple[int, str | None]:
    """Phase-9 layer 5: cross-pair currency-strength rank vote.

    Computes 24h relative strength for each of the 8 majors from real Yahoo
    1H closes. For pair AB:
      - if A is in the top-N strongest AND B is in the bottom-N weakest,
        emit +base_pts (BUY pair)
      - if A is in the bottom-N AND B is in the top-N, emit -base_pts (SELL)
      - otherwise 0 (no rank-divergence edge)

    Real cross-pair signal that the per-pair technical stack misses: it
    captures basket-wide currency flow that drove the move on the other
    27 pairs in the same 24h window.
    """
    strengths = _compute_currency_strength_24h()
    if not strengths or len(strengths) < 4:
        return 0, None
    base = pair[:3]
    quote = pair[3:6]
    base_s = strengths.get(base)
    quote_s = strengths.get(quote)
    if base_s is None or quote_s is None:
        return 0, None
    # Rank ascending: index 0 = weakest, last = strongest
    ranking = sorted(strengths.items(), key=lambda kv: kv[1])
    rank: dict[str, int] = {ccy: i for i, (ccy, _) in enumerate(ranking)}
    n = len(ranking)
    top_thr = max(1, n - 3)  # top 3 strongest = ranks [n-3, n-1]
    bot_thr = 2  # bottom 3 weakest = ranks [0, 1, 2]
    base_rank = rank[base]
    quote_rank = rank[quote]
    # Strong BUY when base in top, quote in bottom
    if base_rank >= top_thr and quote_rank <= bot_thr:
        sign = +1
    elif base_rank <= bot_thr and quote_rank >= top_thr:
        sign = -1
    else:
        return 0, None
    delta = base_pts * sign
    return delta, (
        f"currency_strength: {base}(rank{base_rank+1}/{n}, {base_s*100:+.2f}%) vs "
        f"{quote}(rank{quote_rank+1}/{n}, {quote_s*100:+.2f}%) → {sign:+d}×{base_pts}"
    )


def multi_event_cluster_amplifier(
    pair: str, session: str, now_utc: datetime,
    window_hours: int = WINDOW_HOURS, base_event_score: int = 4,
) -> tuple[int, str | None]:
    """Phase-8 layer 3: when ≥ 2 persistent-driver events co-fire AND agree
    on direction, add a small extra boost on top of the base event-score.

    This captures situations like 'US NFP + US Unemployment both bearish-USD
    in same Friday window' — the signal is unusually strong because two
    fundamentals align. We award +base_event_score (max +base_event_score*2)
    in the agreed direction.
    """
    _load()
    session = _norm_session(session, hour=now_utc.hour)
    events = persistent_events_in_window(pair, now_utc, window_hours)
    if len(events) < 2:
        return 0, None
    # Sum signed contributions per matched (pair,session,event_type)
    signs: list[int] = []
    for e in events:
        meta = _per_event_ps.get((pair, session, e["type"]))
        if not meta or meta["frequency"] < 2 or meta["concordance"] < 65:
            continue
        s = _direction_to_pair_sign(meta["dominant_direction_event_ccy"], e["currency"], pair)
        if s != 0:
            signs.append(s)
    if len(signs) < 2:
        return 0, None
    pos = sum(1 for s in signs if s > 0)
    neg = sum(1 for s in signs if s < 0)
    # Need at least 2 events agreeing (one direction outweighs ~75% of votes)
    if max(pos, neg) < 2 or max(pos, neg) / len(signs) < 0.75:
        return 0, None
    final_sign = +1 if pos >= neg else -1
    # Magnitude scales with cluster size: 2 events → +base, 3+ events → +2*base
    magnitude = base_event_score if len(signs) <= 2 else min(2 * base_event_score, base_event_score + len(signs))
    delta = magnitude * final_sign
    return delta, f"multi_event_cluster: {len(signs)} aligned events × {final_sign:+d} → {delta:+d}"


def trap_score_penalty(pair: str, session: str, score: int, threshold_pct: float = 50.0) -> tuple[int, str | None]:
    """Soft trap-filter: if (pair, session) trap-rate ≥ threshold_pct, return
    a small penalty that REDUCES |score|. Does NOT reverse direction or zero
    out trades — paper_trader's free 70% gate decides openings; this only
    nudges probability down a touch on known whipsaw cells.

    Returns (delta_score_int, reason_string) or (0, None).
    """
    _load()
    session = _norm_session(session)
    rate = trap_risk(pair, session)
    if rate < threshold_pct or score == 0:
        return 0, None
    # Penalty proportional to how much trap-rate exceeds threshold.
    # rate=50 → 0pts (boundary); rate=90 → up to 4pts off.
    excess = (rate - threshold_pct) / 50.0  # 0..1 over [50,100]
    penalty = max(1, int(round(4 * excess)))
    penalty = min(penalty, abs(score))  # never flip the sign
    delta = -penalty if score > 0 else penalty
    return delta, f"trap_filter: cell trap_rate={rate:.0f}% (threshold {threshold_pct:.0f}%), reducing |score| by {penalty}"
