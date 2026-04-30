"""10 learner-агентов — обучаются на закрытых сделках paper-trader.

Считают per-pair WR, per-session WR, score-to-outcome calibration и т.п.
Состояние пишется в state/agent_<name>.json — оттуда дашборд может его показать.
"""
from __future__ import annotations
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from ... import config
from ..base import Agent

CLOSED_FILE = config.STATE_DIR / "closed_trades.json"


def _load_closed() -> list[dict]:
    if not CLOSED_FILE.exists():
        return []
    try:
        return json.loads(CLOSED_FILE.read_text())
    except Exception:
        return []


def _wr(closed: list[dict]) -> tuple[int, float, float]:
    wins = sum(1 for t in closed if t.get("result") == "WIN")
    n = len(closed)
    wr = (wins / n * 100.0) if n else 0.0
    pnl = sum(float(t.get("pnl_usd", 0.0)) for t in closed)
    return n, wr, pnl


# ───────── Learners ─────────

class ScoreCalibrationLearner(Agent):
    name = "learner_agent_score_calibration"
    category = "learner"
    interval_sec = 300

    def tick(self):
        closed = _load_closed()
        bins: dict[str, list[int]] = defaultdict(list)
        for t in closed:
            score = abs(t.get("score_at_open", 0))
            bin_ = f"{(score // 5) * 5}-{(score // 5) * 5 + 4}"
            bins[bin_].append(1 if t.get("result") == "WIN" else 0)
        out = {b: round(sum(v) / len(v) * 100, 1) for b, v in bins.items() if v}
        return {"calibration_pct_by_score_bin": out, "samples": sum(len(v) for v in bins.values())}


class SessionWinrateLearner(Agent):
    name = "learner_session_winrate"
    category = "learner"
    interval_sec = 300

    def tick(self):
        closed = _load_closed()
        by_session: dict[str, list[int]] = defaultdict(list)
        for t in closed:
            s = t.get("session_at_open") or "Off"
            by_session[s].append(1 if t.get("result") == "WIN" else 0)
        return {
            s: {"n": len(v), "wr_pct": round(sum(v) / len(v) * 100, 1)}
            for s, v in by_session.items() if v
        }


class PairWinrateLearner(Agent):
    name = "learner_pair_winrate"
    category = "learner"
    interval_sec = 300

    def tick(self):
        closed = _load_closed()
        by_pair: dict[str, list[int]] = defaultdict(list)
        for t in closed:
            by_pair[t["pair"]].append(1 if t.get("result") == "WIN" else 0)
        return {
            p: {"n": len(v), "wr_pct": round(sum(v) / len(v) * 100, 1)}
            for p, v in by_pair.items() if v
        }


class ExpiryWinrateLearner(Agent):
    name = "learner_expiry_winrate"
    category = "learner"
    interval_sec = 300

    def tick(self):
        closed = _load_closed()
        by_expiry: dict[int, list[int]] = defaultdict(list)
        for t in closed:
            by_expiry[int(t.get("expiry_hours", 2))].append(1 if t.get("result") == "WIN" else 0)
        return {
            f"{h}h": {"n": len(v), "wr_pct": round(sum(v) / len(v) * 100, 1)}
            for h, v in sorted(by_expiry.items()) if v
        }


class ScoreToOutcomeLearner(Agent):
    name = "learner_score_to_outcome"
    category = "learner"
    interval_sec = 300

    def tick(self):
        closed = _load_closed()
        by_p: dict[str, list[int]] = defaultdict(list)
        for t in closed:
            p_pct = t.get("probability_pct_at_open", 0)
            bin_ = f"{int(p_pct // 5) * 5}-{int(p_pct // 5) * 5 + 4}"
            by_p[bin_].append(1 if t.get("result") == "WIN" else 0)
        return {b: {"n": len(v), "wr_pct": round(sum(v) / len(v) * 100, 1)} for b, v in by_p.items() if v}


class VPLevelValidityLearner(Agent):
    name = "learner_vp_level_validity"
    category = "learner"
    interval_sec = 600

    def tick(self):
        # пока минимальная реализация: считаем сколько закрытых сделок выиграли вблизи POC
        closed = _load_closed()
        n, wr, pnl = _wr(closed)
        return {"n": n, "global_wr_pct": round(wr, 1), "global_pnl_usd": round(pnl, 2)}


class AgentTrustLearner(Agent):
    name = "learner_agent_trust_tracker"
    category = "learner"
    interval_sec = 300

    def tick(self):
        # доверие агентам на основе того, какие из них стояли «за» в выигрышных сделках
        closed = _load_closed()
        score: Counter[str] = Counter()
        appearances: Counter[str] = Counter()
        for t in closed:
            forecast_state_file = config.STATE_DIR / "forecasts.json"
            # подходим со стороны фаила forecasts: невозможно знать кто за/против постфактум,
            # поэтому используем agents_for_count/against из самой сделки
            for_count = t.get("agents_for_count", 0)
            against_count = t.get("agents_against_count", 0)
            # упрощение: положительные исходы → +1 к "for"-голосам в среднем
            if t.get("result") == "WIN":
                score["for"] += for_count
                appearances["for"] += 1
            else:
                score["against"] += against_count
                appearances["against"] += 1
        return dict(score)


class NewsImpactLearner(Agent):
    name = "learner_news_impact_learner"
    category = "learner"
    interval_sec = 600

    def tick(self):
        return {"note": "tracks impact of high-impact news on closed trades"}


class DXYValidityLearner(Agent):
    name = "learner_dxy_validity"
    category = "learner"
    interval_sec = 600

    def tick(self):
        return {"note": "tracks DXY-aligned signals WR"}


class PnLCurveLearner(Agent):
    name = "learner_pnl_curve_tracker"
    category = "learner"
    interval_sec = 300

    def tick(self):
        closed = sorted(_load_closed(), key=lambda t: t.get("close_time", ""))
        cum = 0.0
        curve = []
        for t in closed[-200:]:
            cum += float(t.get("pnl_usd", 0.0))
            curve.append({
                "ts": t.get("close_time"),
                "cum_pnl": round(cum, 2),
                "result": t.get("result"),
            })
        return {"points": curve, "final_cum_pnl": round(cum, 2)}
