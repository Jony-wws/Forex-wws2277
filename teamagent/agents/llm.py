"""3 LLM-агента (Groq Llama, Google Gemini, OpenRouter DeepSeek/Qwen).

Если ключи не заданы — агенты пишут heartbeat с status='no_api_key' и просто спят
без вызовов. Никаких заглушек/фейковых ответов.

Каждый тик:
- читает state/forecasts.json (топ-3 пары)
- спрашивает модель «оцени, согласна ли с прогнозом для пары X»
- сохраняет ответ в state/agent_<name>.json
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone

from .. import config
from .base import Agent


def _topk_forecasts(k: int = 3) -> list[dict]:
    fp = config.STATE_DIR / "forecasts.json"
    if not fp.exists():
        return []
    try:
        snap = json.loads(fp.read_text())
        return snap.get("rankings", [])[:k]
    except Exception:
        return []


def _build_prompt(forecast: dict) -> str:
    return (
        f"Trading forecast for {forecast['pair']}: side={forecast['side']}, "
        f"probability={forecast['probability_pct']}%, score={forecast['score']}/44, "
        f"expiry={forecast['recommended_hours']}h. "
        "In one short JSON object with fields {agree: bool, confidence_0_100: int, "
        "key_risk: string}, evaluate this forecast based on common forex factors "
        "(macro, session, technicals). Reply with JSON only."
    )


class GroqReasoningAgent(Agent):
    name = "llm_groq_reasoning"
    category = "llm"
    interval_sec = 240

    def tick(self):
        if not config.GROQ_API_KEY:
            return {"status": "no_api_key", "note": "set GROQ_API_KEY"}
        try:
            from groq import Groq
        except ImportError:
            return {"status": "no_groq_lib"}
        client = Groq(api_key=config.GROQ_API_KEY)
        out = []
        for f in _topk_forecasts(3):
            try:
                resp = client.chat.completions.create(
                    model=config.GROQ_MODELS[0],
                    messages=[
                        {"role": "system", "content": "You are an institutional FX trader."},
                        {"role": "user", "content": _build_prompt(f)},
                    ],
                    temperature=0.1,
                    max_tokens=200,
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content
                out.append({"pair": f["pair"], "verdict": content})
            except Exception as e:
                out.append({"pair": f["pair"], "error": str(e)[:200]})
                # rate-limit или другое — спим короче и идём дальше
        return {"status": "ok", "verdicts": out, "model": config.GROQ_MODELS[0]}


class GeminiChartReader(Agent):
    name = "llm_gemini_chart_reader"
    category = "llm"
    interval_sec = 240

    def tick(self):
        if not config.GOOGLE_API_KEY:
            return {"status": "no_api_key", "note": "set GOOGLE_API_KEY"}
        try:
            import google.generativeai as genai
        except ImportError:
            return {"status": "no_gemini_lib"}
        genai.configure(api_key=config.GOOGLE_API_KEY)
        model = genai.GenerativeModel(config.GEMINI_MODELS[0])
        out = []
        for f in _topk_forecasts(3):
            try:
                resp = model.generate_content(_build_prompt(f))
                out.append({"pair": f["pair"], "verdict": resp.text[:500]})
            except Exception as e:
                out.append({"pair": f["pair"], "error": str(e)[:200]})
        return {"status": "ok", "verdicts": out, "model": config.GEMINI_MODELS[0]}


class OpenRouterConsensusAgent(Agent):
    name = "llm_openrouter_consensus"
    category = "llm"
    interval_sec = 300

    def tick(self):
        if not config.OPENROUTER_API_KEY:
            return {"status": "no_api_key", "note": "set OPENROUTER_API_KEY"}
        try:
            from openai import OpenAI
        except ImportError:
            return {"status": "no_openai_lib"}
        client = OpenAI(
            api_key=config.OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
        )
        out = []
        for f in _topk_forecasts(3):
            try:
                resp = client.chat.completions.create(
                    model=config.OPENROUTER_MODELS[0],
                    messages=[
                        {"role": "system", "content": "You are an institutional FX trader."},
                        {"role": "user", "content": _build_prompt(f)},
                    ],
                    temperature=0.1,
                    max_tokens=200,
                )
                out.append({"pair": f["pair"], "verdict": resp.choices[0].message.content})
            except Exception as e:
                out.append({"pair": f["pair"], "error": str(e)[:200]})
        return {"status": "ok", "verdicts": out, "model": config.OPENROUTER_MODELS[0]}
