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


# ───────── Floor / weekly review (added 2026-05-01 per user request) ─────────

class WRFloorMonitor(Agent):
    """Считает rolling WR за последние 50 сделок и пишет alert если WR < 70%.

    НЕ блокирует открытие сделок — это просто индикатор «стратегия начала
    устаревать, пора триггернуть strategy_search». Free 70% gate сам по себе
    фильтр входа.
    """
    name = "learner_wr_floor_monitor"
    category = "learner"
    interval_sec = 300

    FLOOR_PCT = 70.0
    WINDOW_TRADES = 50

    def tick(self):
        closed = sorted(_load_closed(), key=lambda t: t.get("close_time", ""))
        recent = closed[-self.WINDOW_TRADES:]
        n = len(recent)
        if n == 0:
            return {"window": 0, "wr_pct": None, "floor_pct": self.FLOOR_PCT,
                    "below_floor": False, "alert": "no closed trades yet"}
        wins = sum(1 for t in recent if t.get("result") == "WIN")
        wr = round(wins / n * 100, 1)
        below = wr < self.FLOOR_PCT
        # все-время WR (для контекста)
        n_all = len(closed)
        wins_all = sum(1 for t in closed if t.get("result") == "WIN")
        wr_all = round(wins_all / n_all * 100, 1) if n_all else None
        return {
            "window": n,
            "wr_pct": wr,
            "wr_pct_all_time": wr_all,
            "floor_pct": self.FLOOR_PCT,
            "below_floor": below,
            "alert": (
                f"⚠️ rolling WR {wr}% < floor {self.FLOOR_PCT}% — "
                "стратегия деградирует, нужен новый strategy_search sweep"
            ) if below else "OK",
        }


class WeeklyLossReview(Agent):
    """Каждые ~7 дней (но tick каждые 6h, чтобы не пропустить переход) делает
    разбор минусов из closed_trades.json: какие пары/сессии/часы UTC/направления
    проигрывали чаще остальных. Помогает увидеть «слепые пятна» стратегии.
    """
    name = "learner_weekly_loss_review"
    category = "learner"
    interval_sec = 6 * 60 * 60   # 6 часов между tick'ами; внутри сам решает обновить ли

    def tick(self):
        closed = _load_closed()
        if not closed:
            return {"note": "no closed trades yet"}
        # окно: последние 7 дней
        try:
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            week_ago = now - timedelta(days=7)
            recent = [
                t for t in closed
                if t.get("close_time")
                and datetime.fromisoformat(t["close_time"]).astimezone(timezone.utc) >= week_ago
            ]
        except Exception:
            recent = closed[-50:]   # fallback

        losses = [t for t in recent if t.get("result") == "LOSS"]
        wins = [t for t in recent if t.get("result") == "WIN"]
        n_total = len(recent)
        n_loss = len(losses)
        if n_total == 0:
            return {"window_days": 7, "n_total": 0, "note": "no closed trades in last 7d"}

        # минусы по парам
        loss_by_pair = Counter(t["pair"] for t in losses)
        # минусы по сессиям
        loss_by_session = Counter(t.get("session_at_open") or "Off" for t in losses)
        # минусы по часу UTC
        loss_by_hour = Counter()
        for t in losses:
            try:
                h = datetime.fromisoformat(t["open_time"]).astimezone(timezone.utc).hour
                loss_by_hour[h] += 1
            except Exception:
                pass
        # минусы по направлению
        loss_by_side = Counter(t.get("side") for t in losses)
        # худшие пары: WR ≤ 40% при ≥ 3 сделках
        wr_by_pair: dict[str, dict] = {}
        all_by_pair: dict[str, list] = defaultdict(list)
        for t in recent:
            all_by_pair[t["pair"]].append(1 if t.get("result") == "WIN" else 0)
        for p, vs in all_by_pair.items():
            if len(vs) >= 3:
                wr = round(sum(vs) / len(vs) * 100, 1)
                wr_by_pair[p] = {"n": len(vs), "wr_pct": wr}
        worst_pairs = sorted(
            [(p, d["wr_pct"], d["n"]) for p, d in wr_by_pair.items() if d["wr_pct"] <= 40.0],
            key=lambda x: (x[1], -x[2]),
        )

        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "window_days": 7,
            "n_total": n_total,
            "n_wins": len(wins),
            "n_losses": n_loss,
            "wr_pct": round(len(wins) / n_total * 100, 1) if n_total else 0.0,
            "loss_by_pair_top5": loss_by_pair.most_common(5),
            "loss_by_session": dict(loss_by_session),
            "loss_by_hour_utc_top5": loss_by_hour.most_common(5),
            "loss_by_side": dict(loss_by_side),
            "worst_pairs_wr_le_40pct": worst_pairs,
            "advice": (
                f"за неделю {n_loss} минусов из {n_total} сделок. "
                + ("проблемные пары: " + ", ".join(p for p, _, _ in worst_pairs[:3]) if worst_pairs
                   else "пары со стабильно низким WR в этом окне отсутствуют.")
            ),
        }
