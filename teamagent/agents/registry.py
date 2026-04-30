"""Реестр всех 60 агентов. Каждый агент = (name, category, module:class).

Структура:
- 28 specialists (один на пару) — pair_specialist:PairSpecialist
- 14 analyzers (разные методологии)
- 10 learners (обучающиеся на истории)
- 5 health/recovery
- 3 LLM agents (только если есть ключи)

Итого 60.
"""
from __future__ import annotations
from .. import config


def all_agents() -> list[dict]:
    """Возвращает список всех 60 агентов с metadata."""
    agents: list[dict] = []

    # ─── 28 specialists ───
    for pair in config.PAIRS:
        agents.append({
            "name": f"specialist_{pair}",
            "category": "specialist",
            "module": "teamagent.agents.specialists.pair_specialist",
            "class": "PairSpecialist",
            "init_args": {"pair": pair},
        })

    # ─── 14 analyzers ───
    analyzer_specs = [
        ("vwap_bias",         "VWAPBiasAnalyzer"),
        ("bbp_regime",        "BBPRegimeAnalyzer"),
        ("rsi_divergence",    "RSIDivergenceAnalyzer"),
        ("trend_alignment",   "TrendAlignmentAnalyzer"),
        ("bb_squeeze",        "BBSqueezeAnalyzer"),
        ("momentum_burst",    "MomentumBurstAnalyzer"),
        ("session_strength",  "SessionStrengthAnalyzer"),
        ("range_break",       "RangeBreakAnalyzer"),
        ("liquidity_sweep",   "LiquiditySweepAnalyzer"),
        ("volatility_regime", "VolatilityRegimeAnalyzer"),
        ("multi_tf_consensus","MultiTFConsensusAnalyzer"),
        ("vp_aggregator",     "VPAggregatorAnalyzer"),
        ("news_filter",       "NewsFilterAnalyzer"),
        ("dxy_alignment",     "DXYAlignmentAnalyzer"),
    ]
    for name, cls in analyzer_specs:
        agents.append({
            "name": f"analyzer_{name}",
            "category": "analyzer",
            "module": "teamagent.agents.analyzers.builtin",
            "class": cls,
            "init_args": {},
        })

    # ─── 10 learners ───
    learner_specs = [
        ("agent_score_calibration", "ScoreCalibrationLearner"),
        ("session_winrate",         "SessionWinrateLearner"),
        ("pair_winrate",            "PairWinrateLearner"),
        ("expiry_winrate",          "ExpiryWinrateLearner"),
        ("score_to_outcome",        "ScoreToOutcomeLearner"),
        ("vp_level_validity",       "VPLevelValidityLearner"),
        ("agent_trust_tracker",     "AgentTrustLearner"),
        ("news_impact_learner",     "NewsImpactLearner"),
        ("dxy_validity",            "DXYValidityLearner"),
        ("pnl_curve_tracker",       "PnLCurveLearner"),
    ]
    for name, cls in learner_specs:
        agents.append({
            "name": f"learner_{name}",
            "category": "learner",
            "module": "teamagent.agents.learners.builtin",
            "class": cls,
            "init_args": {},
        })

    # ─── 5 health/recovery ───
    health_specs = [
        ("recovery_supervisor", "RecoverySupervisor"),
        ("memory_doctor",       "MemoryDoctor"),
        ("data_freshness",      "DataFreshnessChecker"),
        ("disk_janitor",        "DiskJanitor"),
        ("api_health_pinger",   "APIHealthPinger"),
    ]
    for name, cls in health_specs:
        agents.append({
            "name": f"health_{name}",
            "category": "health",
            "module": "teamagent.agents.health.builtin",
            "class": cls,
            "init_args": {},
        })

    # ─── 3 LLM agents ───
    llm_specs = [
        ("groq_reasoning",        "GroqReasoningAgent"),
        ("gemini_chart_reader",   "GeminiChartReader"),
        ("openrouter_consensus",  "OpenRouterConsensusAgent"),
    ]
    for name, cls in llm_specs:
        agents.append({
            "name": f"llm_{name}",
            "category": "llm",
            "module": "teamagent.agents.llm",
            "class": cls,
            "init_args": {},
        })

    assert len(agents) == 60, f"expected 60 agents, got {len(agents)}"
    return agents
