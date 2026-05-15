"""Глубокий AI-анализ ТОП-1 валютной пары + 3 скриншота TradingView в Telegram.

Запускается после каждого 5-часового цикла brain (cron в
``.github/workflows/top1_ai_analysis.yml``).  Скрипт делает то, чего
не делает ``ai_narrative.py``: вместо одного абзаца обзора по всему
рынку он пишет полноценный экспертный разбор одной пары — той,
которую AI brain считает Top-1 — с учётом того, что это
**бинарный опцион на 5 часов** (без TP/SL, только время экспирации).

Шаги:

1. Импортирует ``select_top1`` из ``app.brain`` и получает ТОП-1.
2. Собирает все 7 слоёв (technical, confluence, macro, big_players,
   fundamental, news, sentiment, political), технические индикаторы,
   live_forecast (5h projection), multi-TF alignment, persistence,
   trend strength, edge_check и tier (★ PREMIUM / ⚡ STRONG / ⊙ NORMAL).
3. Формирует развёрнутый русскоязычный prompt и просит LLM
   (Cloudflare Workers AI Llama 3.3 70B → GitHub Models gpt-4o-mini)
   написать живой, неконвейерный анализ "уровня GPT-5.5".
4. Делает 3 PNG-скриншота TradingView (M15, H1, H4) для Top-1 пары с
   индикаторами RSI / MACD / Bollinger / Stochastic через Playwright
   и встроенный TradingView Widget.
5. Шлёт в Telegram сначала текст через ``sendMessage``, затем 3 фото
   как альбом через ``sendMediaGroup``.

Конфигурируется секретами:

* ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` — обязательны для
  отправки.  Без них скрипт просто пишет анализ в
  ``reports/top1_ai_analysis_latest.md`` и завершается успешно.
* ``CF_AI_ACCOUNT_ID`` / ``CF_AI_API_TOKEN`` — опциональны
  (Cloudflare Workers AI, Llama 3.3 70B).  Если отсутствуют — падаем
  на GitHub Models.
* ``GITHUB_TOKEN`` — авто-инжектится в Actions, используется как
  fallback (gpt-4o-mini).
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORTS = ROOT / "reports"
SCREENSHOTS = ROOT / "screenshots" / "top1_ai"
REPORTS.mkdir(parents=True, exist_ok=True)
SCREENSHOTS.mkdir(parents=True, exist_ok=True)

OUT_REPORT = REPORTS / "top1_ai_analysis_latest.md"


# ── LLM ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
Ты — эксперт по бинарным опционам уровня GPT-5.5 с глубоким пониманием форекс-рынка.
Тебе дают полную информацию о ТОП-1 валютной паре из 28, выбранной AI-системой,
плюс глобальный макрофон, матрицу силы 8 валют и Топ-3..5 параллельных кандидатов.

ВАЖНО: Это БИНАРНЫЕ ОПЦИОНЫ — нет Take Profit, нет Stop Loss, только время экспирации (5 часов).
Твоя задача — проанализировать все данные и дать прогноз BUY/SELL на 5 часов.

КРИТИЧЕСКОЕ ПРАВИЛО — блок "# РЕШЕНИЕ СИСТЕМЫ ПО ВХОДУ В СДЕЛКУ":
- Если Decision = GO — дай уверенный разбор и рекомендуй ОТКРЫТЬ сделку СЕЙЧАС
  с экспирацией в переданное время (entry_at + 5ч). Объясни куда пойдёт цена и почему.
- Если Decision = WAIT — НЕ уговаривай пользователя торговать. Честно объясни
  почему фильтр не пройден (gate_ok, tier, conf vs floor, MTF, ADX, news),
  что нужно мониторить до следующего цикла, и есть ли в Топ-5 кто-то сильнее.
  Если в Топ-5 ничего стоящего нет — так и скажи: цикл слабый, ждём.

Требования к анализу:
1. Начни с решения (GO → покупка/продажа с уверенностью, WAIT → «НЕ входим, потому что …»)
2. Опиши техническую картину (мульти-TF, ADX, persistence, ADR %,
   SMC: FVG / Order Blocks / Liquidity sweep, Wyckoff, Volume Profile — POC/VAH/VAL)
3. Прокомментируй order-flow: кумулятивную дельту, пулы BSL/SSL,
   VWAP-отклонение, имбаланс стакана — совпадает ли с направлением или против.
4. Объясни макро-контекст (DXY/US10Y/VIX/Gold/Brent), матрицу силы 8 валют,
   корреляцию пары с DXY и флаг Risk-On/Risk-Off.
5. Smart Money — COT institutional positioning (лучше SSI, потому что это
   реальные позиции крупных игроков на CME).
6. Оцени риски (новости, геополитика, волатильность, спред).
7. Дай прогноз на 5 часов: куда пойдёт цена и почему.
8. Пиши на русском, НЕ шаблонно, как живой аналитик; проф. терминология.
9. Будь честным. Если сигнал слабый — скажи и предложи альтернативу из Топ-5
   (если есть). Никогда не придумывай данные вне того, что передано в промпте.

Формат: структурированный анализ с эмодзи, 1500-2200 символов.
"""


def _call_cloudflare(prompt: str, account_id: str, api_token: str, model: str) -> str | None:
    """Cloudflare Workers AI — основной нарратор (Llama 3.3 70B, free)."""
    body = json.dumps({
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2000,
        "temperature": 0.6,
    }).encode("utf-8")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    req = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_token}",
            "content-type": "application/json",
            "accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"[top1_ai] cloudflare failed: {e}", file=sys.stderr)
        return None
    if not data.get("success"):
        print(f"[top1_ai] cloudflare errors: {data.get('errors')}", file=sys.stderr)
        return None
    result = data.get("result") or {}
    txt = result.get("response")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    choices = result.get("choices") or []
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        if isinstance(msg.get("content"), str):
            return msg["content"].strip()
    return None


def _call_github_models(prompt: str, token: str, model: str) -> str | None:
    """GitHub Models — fallback (gpt-4o-mini, free via auto-injected GITHUB_TOKEN)."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2000,
        "temperature": 0.6,
    }).encode("utf-8")
    req = Request(
        "https://models.github.ai/inference/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
        return ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content")
    except Exception as e:
        print(f"[top1_ai] github models failed: {e}", file=sys.stderr)
        return None


def generate_ai_analysis(prompt: str) -> tuple[str, str]:
    """Run LLM. Returns (text, model_label)."""
    cf_account = os.getenv("CF_AI_ACCOUNT_ID")
    cf_token = os.getenv("CF_AI_API_TOKEN")
    cf_model = os.getenv("CF_AI_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    if cf_account and cf_token:
        out = _call_cloudflare(prompt, cf_account, cf_token, cf_model)
        if out:
            return out, f"Cloudflare · {cf_model}"

    gh_token = os.getenv("GITHUB_TOKEN")
    gh_model = os.getenv("GITHUB_MODEL", "openai/gpt-4o-mini")
    if gh_token:
        out = _call_github_models(prompt, gh_token, gh_model)
        if out:
            return out, f"GitHub Models · {gh_model}"

    return ("(LLM недоступен — нет CF_AI_* и GITHUB_TOKEN.  "
            "Повторим на следующем цикле.)", "no-llm")


# ── Prompt builder ─────────────────────────────────────────────────────


def _fmt_price(p) -> str:
    if p is None:
        return "—"
    try:
        v = float(p)
    except (TypeError, ValueError):
        return str(p)
    return f"{v:.5f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.3f}"


def _fmt_pct(p, digits: int = 1) -> str:
    if p is None:
        return "—"
    try:
        return f"{float(p):.{digits}f}%"
    except (TypeError, ValueError):
        return str(p)


def _side_ru(side: str | None) -> str:
    if side == "BUY":
        return "BUY (ПОКУПКА)"
    if side == "SELL":
        return "SELL (ПРОДАЖА)"
    return "—"


def _tier_label(tier: str | None) -> str:
    return {
        "premium": "★ PREMIUM",
        "strong": "⚡ STRONG",
        "normal": "⊙ NORMAL",
    }.get((tier or "").lower(), tier or "—")


def _utc_plus_5(dt_iso: str | None) -> str:
    if not dt_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
    except ValueError:
        return dt_iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone(timedelta(hours=5)))
            .strftime("%Y-%m-%d %H:%M UTC+5"))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_dt_plus_5(dt: datetime | None) -> str:
    """Format a tz-aware datetime as 'YYYY-MM-DD HH:MM UTC+5'."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=5))).strftime("%Y-%m-%d %H:%M UTC+5")


# 5h cycle boundaries in UTC (corresponds to 00, 05, 10, 15, 20 UTC+5).
_STRICT_5H_UTC_HOURS = (0, 5, 10, 15, 19)


def _next_5h_boundary(now: datetime) -> datetime:
    """Return the next strict 5h cycle boundary (in UTC) strictly after `now`."""
    now_utc = now.astimezone(timezone.utc)
    cands: list[datetime] = []
    for h in _STRICT_5H_UTC_HOURS:
        for off in (0, 1):
            c = now_utc.replace(hour=h, minute=0, second=0, microsecond=0) \
                + timedelta(days=off)
            if c > now_utc:
                cands.append(c)
    return min(cands)


def _compute_entry_window(payload: dict) -> dict:
    """Decide whether to open a new 5h binary option right now (GO) or WAIT.

    Rules:
    - GO  : favorite_check.ok = True AND tier ∈ {premium,strong,normal}
            AND confidence ≥ floor AND side ∈ {BUY,SELL}.
            entry = now, expiry = now + 5h.
    - WAIT: otherwise. entry = next strict 5h cycle boundary (rolling),
            expiry = entry + 5h.  We don't urge the user to trade.

    'now' comes from payload['generated_at_utc'] so all rendering
    functions agree on a single timestamp.
    """
    gen_iso = payload.get("generated_at_utc")
    now = _now_utc()
    if gen_iso:
        try:
            parsed = datetime.fromisoformat(gen_iso.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            now = parsed
        except ValueError:
            pass

    fav = payload.get("favorite_check") or {}
    top1 = payload.get("top1") or payload.get("leading_candidate") or {}
    tier_raw = (top1.get("tier") or "").lower()
    side_raw = (top1.get("side") or "").upper()

    try:
        conf = float(top1.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    conf_pct = conf * 100.0 if conf <= 1.0 else conf

    try:
        floor = float(fav.get("confidence_floor") or 80)
    except (TypeError, ValueError):
        floor = 80.0

    gate_ok = bool(fav.get("ok"))
    tier_ok = tier_raw in ("premium", "strong", "normal")
    conf_ok = conf_pct >= floor
    side_ok = side_raw in ("BUY", "SELL")

    decision = "GO" if (gate_ok and tier_ok and conf_ok and side_ok) else "WAIT"

    reasons: list[str] = []
    if not gate_ok:
        reasons.append(f"favorite_check.ok=False ({fav.get('reason') or 'нет причины'})")
    if not tier_ok:
        reasons.append(f"tier={tier_raw or '—'} (нужен PREMIUM/STRONG/NORMAL)")
    if not conf_ok:
        reasons.append(f"confidence={conf_pct:.1f}% ниже floor {floor:.0f}%")
    if not side_ok:
        reasons.append(f"side={side_raw or '—'} (направление не определено)")

    if decision == "GO":
        entry_at = now
        expiry_at = now + timedelta(hours=5)
    else:
        entry_at = _next_5h_boundary(now)
        expiry_at = entry_at + timedelta(hours=5)

    return {
        "decision": decision,
        "now_utc": now,
        "entry_at_utc": entry_at,
        "expiry_at_utc": expiry_at,
        "minutes_to_entry": max(0, int((entry_at - now).total_seconds() // 60)),
        "minutes_to_expiry": max(0, int((expiry_at - now).total_seconds() // 60)),
        "gate_ok": gate_ok,
        "tier_ok": tier_ok,
        "conf_ok": conf_ok,
        "side_ok": side_ok,
        "tier": tier_raw or "—",
        "conf_pct": conf_pct,
        "floor": floor,
        "reasons": reasons,
    }


def _banner(window: dict) -> str:
    """Big GO/WAIT banner line for the top of every Telegram message."""
    if window["decision"] == "GO":
        return ("✅ <b>ВХОД РЕКОМЕНДОВАН</b> · открой бинарный опцион СЕЙЧАС "
                f"(conf {window['conf_pct']:.0f}%, floor {window['floor']:.0f}%)")
    reasons = "; ".join(window["reasons"]) if window["reasons"] else "фильтр не пройден"
    return ("⛔ <b>ВХОД НЕ РЕКОМЕНДОВАН</b> · мониторинг текущего 5h-цикла\n"
            f"   причины: {reasons}")


def _timing_lines(window: dict) -> list[str]:
    """Timestamp + rolling 5h trade window lines."""
    gen = window["now_utc"]
    if window["decision"] == "GO":
        return [
            f"📅 Сгенерировано: {_fmt_dt_plus_5(gen)}",
            f"🎯 Открыть: <b>сейчас</b> ({_fmt_dt_plus_5(window['entry_at_utc'])})",
            f"⏳ Закрытие через 5 ч: <b>{_fmt_dt_plus_5(window['expiry_at_utc'])}</b>",
        ]
    return [
        f"📅 Сгенерировано: {_fmt_dt_plus_5(gen)}",
        f"⏸ Не входим. Следующая попытка: "
        f"<b>{_fmt_dt_plus_5(window['entry_at_utc'])}</b> "
        f"(через {window['minutes_to_entry']} мин)",
    ]


def _arrow(delta: float | None) -> str:
    if delta is None:
        return "→"
    try:
        d = float(delta)
    except (TypeError, ValueError):
        return "→"
    if d > 0.15:
        return "↑"
    if d > 0.03:
        return "↗"
    if d < -0.15:
        return "↓"
    if d < -0.03:
        return "↘"
    return "→"


CURRENCY_BUCKET = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD")


def _macro_block(payload: dict) -> list[str]:
    """Format the global macro snapshot (DXY/US10Y/VIX/Gold/Brent)."""
    macro_raw = (payload.get("macro") or {}).get("tickers") or {}
    if not macro_raw and "macro_raw" in payload:
        macro_raw = payload.get("macro_raw") or {}

    def row(label: str, key: str, suffix: str = "") -> str:
        v = macro_raw.get(key)
        if v is None:
            return f"  • {label}: n/a"
        return f"  • {label}: Δ5b {float(v):+.2f}%{suffix} {_arrow(v)}"

    lines = ["🌐 <b>ГЛОБАЛЬНЫЙ МАКРОФОН</b> (Δ за 5 баров H1)"]
    lines.append(row("DXY (индекс доллара)", "DXY"))
    lines.append(row("US10Y (10Y treasury)", "US10Y"))
    lines.append(row("VIX (страх)", "VIX"))
    lines.append(row("Золото (XAU/USD)", "GOLD"))
    lines.append(row("Нефть (Brent)", "BRENT"))
    return lines


def _currency_strength_block(payload: dict) -> list[str]:
    """Format the 8-currency strength matrix sorted from strong → weak."""
    strength = (payload.get("macro") or {}).get("currency_strength") or {}
    if not strength:
        return ["💪 <b>МАТРИЦА СИЛЫ ВАЛЮТ</b>: n/a"]
    rows = [(c, float(strength.get(c, 0.0) or 0.0)) for c in CURRENCY_BUCKET]
    rows.sort(key=lambda kv: -kv[1])
    lines = ["💪 <b>МАТРИЦА СИЛЫ ВАЛЮТ</b> (сильная → слабая)"]
    for i, (c, sc) in enumerate(rows, 1):
        lines.append(f"  {i}. {c}: {sc:+.2f} {_arrow(sc)}")
    return lines


def _pair_oneliner(cand: dict, idx: int) -> str:
    """One-line summary for a Top-N candidate in the scanner message."""
    pair = cand.get("pair", "?")
    side = cand.get("side") or "—"
    conf = cand.get("confidence")
    tier = _tier_label(cand.get("tier"))
    layers = cand.get("layers") or {}
    ta = layers.get("technical") or {}
    adx = ta.get("adx_h1")
    persistence = ta.get("persistence_5h")
    mtf = ta.get("multi_tf_aligned")
    safety = (layers.get("safety_5h") or {}).get("projection") or {}
    news = layers.get("news") or {}
    next_evt = news.get("next_event") or {}
    minutes = next_evt.get("minutes") if isinstance(next_evt, dict) else None
    atr = (cand.get("levels") or {}).get("atr_h1") or safety.get("atr")
    parts = [
        f"<b>{idx}. {pair}</b> · {side} · {_fmt_pct(conf)} · {tier}",
        f"   ADX H1={adx if adx is not None else '—'} · "
        f"persistence={_fmt_pct(persistence)} · "
        f"MTF aligned={'да' if mtf else 'нет'}",
    ]
    safety_status = "✓" if safety.get("passes") else "✗"
    atr_str = f"ATR(H1)={_fmt_price(atr)}" if atr is not None else "ATR=—"
    parts.append(f"   {atr_str} · safety {safety_status} · "
                 f"новости {'через ' + str(int(minutes)) + ' мин' if isinstance(minutes, (int, float)) else 'нет'}")
    return "\n".join(parts)


def _orderflow_scanner_block(payload: dict, pair: str | None) -> list[str]:
    """🎯 ORDER FLOW · Top-1 — VSA + SMC снимок для выбранной пары.

    Поля: кумулятивная дельта (синтетическая), пулы ликвидности BSL/SSL,
    FVG H1 + H4, POC/VAH/VAL (H1), VWAP-отклонение в ATR-единицах,
    Liquidity Sweep, свежий Order Block, имбаланс стакана (bid/ask depth).
    """
    out = ["🎯 <b>ORDER FLOW · Top-1</b>" + (f" · {pair}" if pair else "")]
    if not pair:
        out.append("  (нет Top-1 — пропускаем)")
        return out

    top1 = payload.get("top1") or payload.get("leading_candidate") or {}
    layers = top1.get("layers") or {}
    extras = (layers.get("technical") or {}).get("extras") or {}
    smc = extras.get("smc") or {}
    vp = extras.get("vp") or {}
    atr_h1 = (top1.get("levels") or {}).get("atr_h1")

    # Fresh H1 bars for BSL/SSL + VWAP dev + synthetic delta.
    bsl = ssl_lvl = None
    vwap_dev_atr = None
    cum_delta_pct = None
    fvg_h1 = None
    try:
        from app.prices import fetch_bars
        from app.indicators import vwap as vwap_series
        from app.smc import detect_fvgs
        bars_h1 = fetch_bars(pair, "1h", "1mo")
        if bars_h1 is not None and not bars_h1.empty and len(bars_h1) >= 20:
            tail = bars_h1.tail(20)
            bsl = float(tail["High"].max())
            ssl_lvl = float(tail["Low"].min())
            last_close = float(bars_h1["Close"].iloc[-1])
            try:
                vw = vwap_series(bars_h1)
                vw_last = float(vw.iloc[-1])
                if atr_h1 and float(atr_h1) > 0:
                    vwap_dev_atr = (last_close - vw_last) / float(atr_h1)
            except Exception:
                pass

            n = min(50, len(bars_h1))
            tail50 = bars_h1.tail(n)
            opens = tail50["Open"].to_numpy()
            closes = tail50["Close"].to_numpy()
            highs = tail50["High"].to_numpy()
            lows = tail50["Low"].to_numpy()
            sign = (closes >= opens).astype(int) * 2 - 1  # +1 bull / -1 bear
            if "Volume" in tail50.columns:
                vol = tail50["Volume"].to_numpy()
                vol_total = float(abs(vol).sum())
                if vol_total > 0:
                    cum_delta_pct = float((sign * vol).sum()) / vol_total * 100.0
            if cum_delta_pct is None:
                # Fall back: body / range weighted mean (forex Yahoo volume = 0).
                bodies = closes - opens
                ranges = (highs - lows) + 1e-12
                cum_delta_pct = float((bodies / ranges).mean()) * 100.0

            gh1 = detect_fvgs(bars_h1, max_gaps=3)
            if gh1:
                fvg_h1 = {"top": gh1[0].top, "bottom": gh1[0].bottom,
                          "side": gh1[0].side}
    except Exception as e:
        print(f"[top1_ai] orderflow H1 fetch failed: {e}", file=sys.stderr)

    fvgs_h4 = smc.get("fvgs") or []
    fvg_h4 = fvgs_h4[0] if fvgs_h4 else None

    # Order Book Imbalance + spread (approximation from app.orderbook).
    ob_imb_pct = None
    bid = ask = spread_pips = None
    try:
        from app.orderbook import get_orderbook
        ob = get_orderbook(pair) or {}
        bid = ob.get("bid")
        ask = ob.get("ask")
        spread_pips = ob.get("spread_pips")
        depth = ob.get("depth") or []
        bid_vol = sum(float(d.get("volume", 0) or 0)
                      for d in depth if d.get("side") == "bid")
        ask_vol = sum(float(d.get("volume", 0) or 0)
                      for d in depth if d.get("side") == "ask")
        if (bid_vol + ask_vol) > 0:
            ob_imb_pct = (bid_vol - ask_vol) / (bid_vol + ask_vol) * 100.0
    except Exception as e:
        print(f"[top1_ai] orderbook fetch failed: {e}", file=sys.stderr)

    sweep = smc.get("sweep") or {}
    sweep_str = sweep.get("event") or "none"
    sweep_lvl = sweep.get("level")
    obs = smc.get("order_blocks") or []
    fresh_ob = obs[0] if obs else None

    def fvg_str(g):
        if not g:
            return "—"
        side_ru = "бычий" if g["side"] == "bull" else "медвежий"
        return f"{side_ru} {_fmt_price(g['bottom'])}…{_fmt_price(g['top'])}"

    out.append(f"  • Кум. дельта (~50×H1, synth): "
               f"{('%+.1f%%' % cum_delta_pct) if cum_delta_pct is not None else 'n/a'}")
    out.append(f"  • Пулы ликвидности: BSL≈{_fmt_price(bsl) if bsl is not None else '—'} "
               f"· SSL≈{_fmt_price(ssl_lvl) if ssl_lvl is not None else '—'}")
    out.append(f"  • FVG H1: {fvg_str(fvg_h1)} · FVG H4: {fvg_str(fvg_h4)}")
    poc = vp.get("poc"); vah = vp.get("vah"); val = vp.get("val")
    out.append(f"  • Volume Profile H1: POC {_fmt_price(poc) if poc is not None else '—'} "
               f"· VAH {_fmt_price(vah) if vah is not None else '—'} "
               f"· VAL {_fmt_price(val) if val is not None else '—'}")
    out.append(f"  • VWAP отклонение: "
               f"{('%+.2f ATR' % vwap_dev_atr) if vwap_dev_atr is not None else 'n/a'}")
    sweep_line = f"  • Liquidity sweep: {sweep_str}"
    if sweep_lvl is not None:
        sweep_line += f" @ {_fmt_price(sweep_lvl)}"
    if fresh_ob:
        ob_side_ru = "бычий" if fresh_ob.get("side") == "bull" else "медвежий"
        sweep_line += (f" · свежий OB {ob_side_ru} "
                       f"{_fmt_price(fresh_ob.get('low'))}…{_fmt_price(fresh_ob.get('high'))}")
    out.append(sweep_line)
    out.append(f"  • Стакан: bid={_fmt_price(bid) if bid else '—'} "
               f"· ask={_fmt_price(ask) if ask else '—'} "
               f"· спред={spread_pips if spread_pips else '—'} пипс "
               f"· имбаланс={('%+.0f%%' % ob_imb_pct) if ob_imb_pct is not None else 'n/a'}")
    return out


def _intermarket_block(payload: dict, pair: str | None) -> list[str]:
    """🌐 INTERMARKET — корреляция пары с DXY (H1, 50 баров) + Risk-On/Off."""
    out = ["🌐 <b>INTERMARKET</b> (корреляция + риск-флаг)"]

    macro_raw = (payload.get("macro") or {}).get("tickers") or {}
    vix_d = macro_raw.get("VIX")
    us10y_d = macro_raw.get("US10Y")
    gold_d = macro_raw.get("GOLD")

    risk_off = (isinstance(vix_d, (int, float)) and vix_d > 0.5
                and isinstance(us10y_d, (int, float)) and us10y_d > 0.1)
    risk_on = (isinstance(vix_d, (int, float)) and vix_d < -0.5
               and isinstance(gold_d, (int, float)) and gold_d < 0)

    if risk_off:
        risk_lbl = "RISK-OFF (VIX↑ + US10Y↑) — осторожно с шортом JPY/CHF"
    elif risk_on:
        risk_lbl = "RISK-ON (VIX↓ + Gold↓) — рисковые валюты в фаворе"
    else:
        risk_lbl = "нейтрально"

    corr_dxy = None
    if pair:
        try:
            import yfinance as yf
            from app.prices import fetch_bars
            pair_h1 = fetch_bars(pair, "1h", "1mo")
            # DXY is not a forex pair, fetch it directly via Yahoo's index symbol.
            dxy_h1 = yf.Ticker("DX-Y.NYB").history(period="1mo", interval="1h",
                                                    auto_adjust=False)
            if (pair_h1 is not None and dxy_h1 is not None
                    and not pair_h1.empty and not dxy_h1.empty):
                p_ret = pair_h1["Close"].pct_change().dropna()
                d_ret = dxy_h1["Close"].pct_change().dropna()
                # Re-localize both indexes to UTC-naive for join.
                if getattr(p_ret.index, "tz", None) is not None:
                    p_ret.index = p_ret.index.tz_convert("UTC").tz_localize(None)
                if getattr(d_ret.index, "tz", None) is not None:
                    d_ret.index = d_ret.index.tz_convert("UTC").tz_localize(None)
                joined = p_ret.to_frame("p").join(d_ret.to_frame("d"), how="inner").tail(50)
                if len(joined) >= 20:
                    corr_dxy = float(joined["p"].corr(joined["d"]))
        except Exception as e:
            print(f"[top1_ai] DXY corr failed: {e}", file=sys.stderr)

    out.append(f"  • Корреляция {pair or '—'} ↔ DXY (50×H1): "
               f"{('%+.2f' % corr_dxy) if corr_dxy is not None else 'n/a'}")
    out.append(f"  • Режим рынка: {risk_lbl}")
    return out


def _execution_block(payload: dict, pair: str | None) -> list[str]:
    """⚙️ EXECUTION — ADX-порог 25, MTF strict, ADR%."""
    out = ["⚙️ <b>EXECUTION CHECK</b> (исполнимость входа)"]
    top1 = payload.get("top1") or payload.get("leading_candidate") or {}
    layers = top1.get("layers") or {}
    ta = layers.get("technical") or {}
    adx = ta.get("adx_h1")
    mtf = ta.get("multi_tf_aligned")
    persistence = ta.get("persistence_5h")

    try:
        adx_val = float(adx) if adx is not None else None
    except (TypeError, ValueError):
        adx_val = None
    adx_pass = adx_val is not None and adx_val >= 25
    adx_str = (f"{adx_val:.1f}" if adx_val is not None else "—")
    adx_lbl = "≥25 ✓" if adx_pass else "<25 ✗ (слабый тренд)"
    out.append(f"  • ADX H1: {adx_str} ({adx_lbl})")

    mtf_lbl = "D1+H4+H1+M15 ✓" if mtf else "не совпадают ✗"
    out.append(f"  • Multi-TF alignment: {mtf_lbl} · persistence 5h: "
               f"{_fmt_pct(persistence)}")

    # ADR % — used range today vs 14-day ADR.
    adr_pct = None
    if pair:
        try:
            from app.prices import fetch_bars
            bars_d1 = fetch_bars(pair, "1d", "1mo")
            if bars_d1 is not None and len(bars_d1) >= 15:
                ranges_14 = (bars_d1["High"] - bars_d1["Low"]).tail(15).head(14)
                adr14 = float(ranges_14.mean())
                today = bars_d1.iloc[-1]
                today_range = float(today["High"] - today["Low"])
                if adr14 > 0:
                    adr_pct = today_range / adr14 * 100.0
        except Exception as e:
            print(f"[top1_ai] ADR calc failed: {e}", file=sys.stderr)
    out.append(f"  • ADR % (пройдено сегодня / 14-day ADR): "
               f"{('%.0f%%' % adr_pct) if adr_pct is not None else 'n/a'}")
    return out


def _sentiment_block(payload: dict) -> list[str]:
    """🧠 SENTIMENT — SSI=n/a + COT positioning из big_players/cot."""
    out = ["🧠 <b>SENTIMENT</b> (позиционирование)"]
    out.append("  • SSI (розничный buy/sell %): n/a (источник недоступен на Yahoo)")
    bp_snap = payload.get("big_players") or {}
    cur_scores = bp_snap.get("currency_scores") or {}
    cot_scores = bp_snap.get("cot_scores") or bp_snap.get("cot") or {}
    if cur_scores:
        rows = sorted(cur_scores.items(), key=lambda kv: -float(kv[1] or 0))[:8]
        compact = " · ".join(f"{k} {float(v or 0):+.2f}" for k, v in rows)
        out.append(f"  • Smart Money (composite): {compact}")
    elif cot_scores:
        rows = sorted(cot_scores.items(), key=lambda kv: -float(kv[1] or 0))[:8]
        compact = " · ".join(f"{k} z={float(v or 0):+.2f}" for k, v in rows)
        out.append(f"  • COT z-score (52-week): {compact}")
    else:
        out.append("  • COT/Smart Money: n/a")
    return out


def build_scanner_message(payload: dict) -> str:
    """Build the first Telegram message: scanner of all 28 pairs.

    Layout (top → bottom):
      banner (GO/WAIT)
      generated_at + entry/expiry timing
      🌐 Macro snapshot (DXY/US10Y/VIX/Gold/Brent)
      💪 Currency strength matrix (8 currencies)
      📊 Top-5 candidates (oneliners)
      🎯 Order flow · Top-1 (VSA + SMC + VP + OB)
      🌐 Intermarket (DXY corr + Risk-On/Off)
      ⚙️ Execution check (ADX, MTF, ADR%)
      🧠 Sentiment (SSI=n/a, COT institutional)
    """
    top5 = payload.get("top5") or []
    top1 = payload.get("top1") or payload.get("leading_candidate") or {}
    pair = top1.get("pair")
    window = _compute_entry_window(payload)

    lines: list[str] = []
    lines.append("📡 <b>СКАНЕР 28 ПАР · 5h binary-option</b>")
    lines.append(_banner(window))
    lines.extend(_timing_lines(window))
    lines.append("")
    lines.extend(_macro_block(payload))
    lines.append("")
    lines.extend(_currency_strength_block(payload))
    lines.append("")
    lines.append("📊 <b>ТОП-5 СИГНАЛОВ</b> (по композитному score brain)")
    take = top5[:5] if top5 else ([top1] if top1 else [])
    for i, cand in enumerate(take, 1):
        lines.append(_pair_oneliner(cand, i))
    if not take:
        lines.append("  (нет кандидатов — все 28 отфильтрованы veto / favorite_check)")
    lines.append("")
    lines.extend(_orderflow_scanner_block(payload, pair))
    lines.append("")
    lines.extend(_intermarket_block(payload, pair))
    lines.append("")
    lines.extend(_execution_block(payload, pair))
    lines.append("")
    lines.extend(_sentiment_block(payload))
    lines.append("")
    lines.append("⤵️ Ниже — подробный AI-разбор Top-1 и 3 графика M15/H1/H4.")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    return text


def _top1_orderflow_block(top1: dict) -> list[str]:
    """Extract order-flow / liquidity bits for the Top-1 pair from extras."""
    layers = top1.get("layers") or {}
    ta_extras = (layers.get("technical") or {}).get("extras") or {}
    smc = ta_extras.get("smc") or {}
    vp = ta_extras.get("vp") or {}
    wy = ta_extras.get("wyckoff") or {}

    lines: list[str] = []
    lines.append("## Поток ордеров / SMC / Volume Profile")
    if smc:
        structure = smc.get("structure") or {}
        sweep = smc.get("sweep") or {}
        fvgs = smc.get("fvgs") or []
        obs = smc.get("order_blocks") or []
        lines.append(f"- Структура H4: {structure.get('event')} (score={structure.get('score')})")
        lines.append(f"- Liquidity sweep: {sweep.get('event')} (level={sweep.get('level')})")
        if obs:
            ob = obs[0]
            lines.append(f"- Свежий Order Block: {ob.get('side')} "
                         f"[{_fmt_price(ob.get('low'))} … {_fmt_price(ob.get('high'))}]")
        if fvgs:
            g = fvgs[0]
            lines.append(f"- Свежий FVG (имбаланс): {g.get('side')} "
                         f"[{_fmt_price(g.get('bottom'))} … {_fmt_price(g.get('top'))}]")
        reasons = smc.get("reasons") or []
        if reasons:
            lines.append("- SMC reasons: " + "; ".join(str(r) for r in reasons[:6]))
    else:
        lines.append("- SMC: нет данных")
    if vp:
        lines.append(f"- Volume Profile (H1, 100 баров): "
                     f"POC={_fmt_price(vp.get('poc'))} · "
                     f"VAH={_fmt_price(vp.get('vah'))} · "
                     f"VAL={_fmt_price(vp.get('val'))} "
                     f"(score={vp.get('score')}, {vp.get('reason') or ''})")
    else:
        lines.append("- Volume Profile: нет данных")
    if wy:
        lines.append(f"- Wyckoff phase: {wy.get('phase') or wy.get('reason')} "
                     f"(score={wy.get('score')})")
    else:
        lines.append("- Wyckoff: нет данных")
    return lines


def build_prompt(payload: dict) -> str:
    top1 = payload.get("top1") or payload.get("leading_candidate") or {}
    if not top1:
        return "(top1 пуст — brain не выбрал ни одной пары)"

    pair = top1.get("pair", "—")
    name_ru = top1.get("name_ru", pair)
    side = top1.get("side")
    conf = top1.get("confidence")
    tier = top1.get("tier")

    layers = top1.get("layers", {}) or {}
    ta = layers.get("technical", {}) or {}
    macro = layers.get("macro", {}) or {}
    big = layers.get("big_players", {}) or {}
    fundamental = layers.get("fundamental", {}) or {}
    news = layers.get("news", {}) or {}
    sentiment = layers.get("sentiment", {}) or {}
    political = layers.get("political", {}) or {}
    confluence = layers.get("confluence", {}) or {}
    senior = layers.get("senior_alignment", {}) or {}
    safety = layers.get("safety_5h", {}) or {}
    edge = top1.get("edge") or layers.get("edge_check") or {}

    live = top1.get("live_forecast") or payload.get("live_forecast") or {}
    proj = (safety.get("projection") or {})

    ta_details = ta.get("details") or []
    ta_extras = ta.get("extras") or {}

    fav = payload.get("favorite_check") or {}
    sentiment_top = payload.get("sentiment") or {}
    political_top = payload.get("political_risk") or {}

    macro_strength = (payload.get("macro") or {}).get("currency_strength") or {}
    macro_strength_lines = []
    for cur, sc in sorted(macro_strength.items(), key=lambda kv: -float(kv[1] or 0))[:8]:
        macro_strength_lines.append(f"{cur}: {sc:+.2f}")

    next_cycle = payload.get("next_cycle_utc") or payload.get("cycle_close_utc")
    minutes_to_expiry = payload.get("minutes_to_expiry")
    window = _compute_entry_window(payload)

    macro_raw = (payload.get("macro") or {}).get("tickers") or {}
    top5 = payload.get("top5") or []

    lines: list[str] = []
    # CRITICAL: this is the system's GO/WAIT decision the LLM MUST respect.
    lines.append("# РЕШЕНИЕ СИСТЕМЫ ПО ВХОДУ В СДЕЛКУ")
    lines.append(f"- Decision: {window['decision']}")
    lines.append(f"- Сгенерировано: {_fmt_dt_plus_5(window['now_utc'])}")
    if window["decision"] == "GO":
        lines.append(f"- Открыть СЕЙЧАС: {_fmt_dt_plus_5(window['entry_at_utc'])}")
        lines.append(f"- Закрытие через 5 ч: {_fmt_dt_plus_5(window['expiry_at_utc'])}")
        lines.append("- ИНСТРУКЦИЯ: Дай уверенный разбор и рекомендуй ОТКРЫТЬ "
                     "сделку СЕЙЧАС. Скажи куда пойдёт цена за 5 часов и почему.")
    else:
        reasons = "; ".join(window["reasons"]) if window["reasons"] else "фильтр не пройден"
        lines.append(f"- Причины НЕ-входа: {reasons}")
        lines.append(f"- Следующая попытка не раньше: "
                     f"{_fmt_dt_plus_5(window['entry_at_utc'])}")
        lines.append("- ИНСТРУКЦИЯ: НЕ уговаривай пользователя торговать. "
                     "Честно объясни почему сейчас пропускаем, что нужно "
                     "мониторить, и есть ли в Топ-5 кто-то сильнее. "
                     "Если в Топ-5 ничего стоящего нет — так и скажи: цикл слабый, ждём.")
    lines.append("")
    # Global context — макро + сила валют + Top-5, чтобы LLM их учитывал.
    lines.append("# ГЛОБАЛЬНЫЙ МАКРОФОН (Δ за 5 баров H1, +% = рост)")
    for label in ("DXY", "US10Y", "VIX", "GOLD", "BRENT"):
        v = macro_raw.get(label)
        lines.append(f"- {label}: {('%+.3f%%' % float(v)) if v is not None else 'n/a'}")
    lines.append("")
    lines.append("# МАТРИЦА СИЛЫ ВАЛЮТ (8 валют, сильная → слабая)")
    if macro_strength_lines:
        lines.append("- " + " · ".join(macro_strength_lines))
    else:
        lines.append("- нет данных")
    lines.append("")
    lines.append("# ТОП-5 ПАРАЛЛЕЛЬНЫХ КАНДИДАТОВ (для сравнения с Top-1)")
    if top5:
        for i, cand in enumerate(top5[:5], 1):
            c_layers = cand.get("layers") or {}
            c_ta = c_layers.get("technical") or {}
            c_safety = (c_layers.get("safety_5h") or {}).get("projection") or {}
            lines.append(
                f"- {i}. {cand.get('pair')} · {cand.get('side')} · "
                f"{_fmt_pct(cand.get('confidence'))} · "
                f"tier={_tier_label(cand.get('tier'))} · "
                f"ADX H1={c_ta.get('adx_h1')} · "
                f"persistence={_fmt_pct(c_ta.get('persistence_5h'))} · "
                f"MTF={bool(c_ta.get('multi_tf_aligned'))} · "
                f"safety_passes={bool(c_safety.get('passes'))}"
            )
    else:
        lines.append("- top5 пуст")
    lines.append("")

    lines.append(f"# ТОП-1 пара: {pair} ({name_ru})")
    lines.append(f"- Направление AI: {_side_ru(side)}")
    lines.append(f"- Уверенность модели: {_fmt_pct(conf)}")
    lines.append(f"- Tier: {_tier_label(tier)}")
    lines.append(f"- Композитный score (raw): {top1.get('composite_score')}")
    lines.append(f"- Veto: {top1.get('veto') or '—'}")
    lines.append(f"- Окно бинарного опциона: 5 часов "
                 f"(до закрытия цикла {_utc_plus_5(next_cycle)}, "
                 f"осталось {minutes_to_expiry} мин.)")
    lines.append("")
    lines.extend(_top1_orderflow_block(top1))
    lines.append("")
    lines.append("## Слой 1 · Technical (multi-TF + индикаторы)")
    lines.append(f"- Direction analyzer: {ta.get('side') or '—'}")
    lines.append(f"- Score: {ta.get('score')} / max {ta.get('max_score')} "
                 f"(confidence {_fmt_pct(ta.get('confidence'))})")
    lines.append(f"- Multi-TF aligned (D1+H4+H1+M15): {bool(ta.get('multi_tf_aligned'))}")
    lines.append(f"- ADX H1: {ta.get('adx_h1')} · Persistence 5h: "
                 f"{_fmt_pct(ta.get('persistence_5h'))}")
    if ta_extras:
        lines.append(f"- Extras (SMC / Wyckoff / VP): {json.dumps(ta_extras, ensure_ascii=False)[:600]}")
    if ta_details:
        lines.append("- Голосования индикаторов (детали из analyzer.py):")
        for d in ta_details[:25]:
            if isinstance(d, dict):
                name = d.get("name") or d.get("block") or "?"
                score = d.get("score")
                reason = d.get("reason") or d.get("explanation") or ""
                lines.append(f"  · {name}: score={score} — {reason}")
            else:
                lines.append(f"  · {d}")
    lines.append("")
    lines.append("## Слой 2 · Confluence (5 TF × 10 индикаторов)")
    lines.append(f"- Side: {confluence.get('side') or '—'}, score: {confluence.get('score')}")
    lines.append(f"- Super-confluence: {bool(confluence.get('super_confluence'))} "
                 f"(bonus={confluence.get('bonus_applied')})")
    reasons = confluence.get("reasons") or []
    if reasons:
        lines.append("- Причины: " + "; ".join(str(r) for r in reasons[:12]))
    lines.append("")
    lines.append("## Слой 3 · Macro (DXY / yields / commodities)")
    lines.append(f"- Score: {macro.get('score')} — {macro.get('reason') or ''}")
    if macro_strength_lines:
        lines.append("- Сила валют (топ-8): " + ", ".join(macro_strength_lines))
    lines.append("")
    lines.append("## Слой 4 · Big players (COT / bid-ask / smart money)")
    lines.append(f"- Score: {big.get('score')} · Side: {big.get('side') or '—'}")
    if big.get("reason"):
        lines.append(f"- Reason: {big.get('reason')}")
    lines.append("")
    lines.append("## Слой 5 · Fundamental (carry / policy rates)")
    lines.append(f"- Score: {fundamental.get('score')} — {fundamental.get('reason') or ''}")
    lines.append("")
    lines.append("## Слой 6 · News (high-impact veto)")
    lines.append(f"- Score: {news.get('score')} — {news.get('reason') or ''}")
    next_events = news.get("next_events") or news.get("events") or []
    if next_events:
        lines.append(f"- Ближайшие события: {json.dumps(next_events, ensure_ascii=False)[:400]}")
    lines.append("")
    lines.append("## Слой 7 · Sentiment & Political risk")
    lines.append(f"- Sentiment (risk-on/off): score={sentiment.get('score')} "
                 f"— {sentiment.get('reason') or ''}")
    lines.append(f"- Politics: score={political.get('score')} — {political.get('reason') or ''}")
    if sentiment_top:
        lines.append(f"- Глобально (сырые): {json.dumps(sentiment_top, ensure_ascii=False)[:300]}")
    if political_top:
        lines.append(f"- Геориски по валютам: "
                     f"{json.dumps(political_top, ensure_ascii=False)[:300]}")
    lines.append("")
    lines.append("## Senior alignment (W1 + M5 + reversal risk H1)")
    lines.append(f"- Weekly aligned: {senior.get('weekly_aligned')} · "
                 f"M5 aligned: {senior.get('m5_aligned')}")
    if senior.get("reason"):
        lines.append(f"- Reason: {senior.get('reason')}")
    lines.append("")
    lines.append("## 5-часовой прогноз (live_forecast / safety projection)")
    if live or proj:
        lines.append(f"- Status RU: {live.get('status_ru') or '—'}")
        lines.append(f"- Entry: {_fmt_price(live.get('entry') or proj.get('entry'))} → "
                     f"Projected close: {_fmt_price(live.get('projected_close') or proj.get('projected_close'))}")
        lines.append(f"- Drift / hour: {live.get('drift_per_hour')} · "
                     f"per minute: {live.get('drift_per_minute')}")
        lines.append(f"- ATR(H1): {live.get('atr_h1') or proj.get('atr')} · "
                     f"Safety margin: {live.get('safety_margin') or proj.get('safety_margin')}")
        lines.append(f"- Stays in profit at expiry: "
                     f"{bool(live.get('stays_in_profit_at_expiry') or proj.get('passes'))}")
        if proj.get("reason"):
            lines.append(f"- Reason: {proj.get('reason')}")
    else:
        lines.append("- Нет данных live_forecast")
    lines.append("")
    lines.append("## Edge check (Wilson 95 % lower-bound на исторической WR)")
    if edge:
        lines.append(f"- Passes: {edge.get('passes')} · Reason: {edge.get('reason') or ''}")
        for k in ("lifetime", "regime", "tier"):
            if edge.get(k) is not None:
                lines.append(f"- {k}: {edge.get(k)}")
    else:
        lines.append("- Edge check не считался (нет истории или veto)")
    lines.append("")
    lines.append("## Favorite check / publication gate")
    lines.append(f"- ok: {fav.get('ok')} · tier: {fav.get('tier')}")
    lines.append(f"- reason: {fav.get('reason') or ''}")
    lines.append(f"- Confidence floor: {fav.get('confidence_floor')}% · "
                 f"Wilson lifetime floor: {fav.get('wilson_lifetime_floor_pct')}%")

    return "\n".join(lines)


# ── Telegram ───────────────────────────────────────────────────────────


def _encode_multipart(fields: dict, files: list[tuple[str, Path, str]]) -> tuple[bytes, str]:
    """Build a multipart/form-data body from a dict of fields and list of files.

    ``files`` items are ``(form_name, path, mime)``.
    """
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        buf.write(str(v).encode("utf-8"))
        buf.write(b"\r\n")
    for form_name, path, mime in files:
        filename = path.name
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="{form_name}"; filename="{filename}"\r\n'
            .encode()
        )
        buf.write(f"Content-Type: {mime}\r\n\r\n".encode())
        buf.write(path.read_bytes())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), boundary


def telegram_send_message(token: str, chat_id: str, text: str) -> bool:
    body = json.dumps({
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            print(f"[top1_ai] sendMessage error: {data}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"[top1_ai] sendMessage failed: {e}", file=sys.stderr)
        return False


def telegram_send_media_group(token: str, chat_id: str, photos: list[Path],
                              captions: list[str]) -> bool:
    """Send a Telegram media group (album) of up to 10 photos.

    Each photo is attached as form-data ``photo<i>`` and referenced from
    the ``media`` JSON array as ``attach://photo<i>``.
    """
    if not photos:
        return False
    media: list[dict] = []
    files: list[tuple[str, Path, str]] = []
    for i, (p, cap) in enumerate(zip(photos, captions)):
        attach = f"photo{i}"
        item: dict = {"type": "photo", "media": f"attach://{attach}"}
        if i == 0 and cap:
            item["caption"] = cap[:1024]
            item["parse_mode"] = "HTML"
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        files.append((attach, p, mime))
        media.append(item)
    fields = {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)}
    body, boundary = _encode_multipart(fields, files)
    req = Request(
        f"https://api.telegram.org/bot{token}/sendMediaGroup",
        data=body,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            print(f"[top1_ai] sendMediaGroup error: {data}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"[top1_ai] sendMediaGroup failed: {e}", file=sys.stderr)
        return False


# ── TradingView screenshots ────────────────────────────────────────────


TV_WIDGET_HTML = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8" />
<title>{symbol} {interval}</title>
<style>
  html, body {{ margin:0; padding:0; background:#131722; height:100%; }}
  #tv {{ width:100vw; height:100vh; }}
</style>
</head><body>
<div id="tv"></div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
  new TradingView.widget({{
    autosize: true,
    symbol: "{symbol}",
    interval: "{interval}",
    timezone: "Etc/UTC",
    theme: "dark",
    style: "1",
    locale: "ru",
    toolbar_bg: "#131722",
    enable_publishing: false,
    hide_top_toolbar: false,
    hide_side_toolbar: true,
    withdateranges: false,
    allow_symbol_change: false,
    studies: [
      "RSI@tv-basicstudies",
      "MACD@tv-basicstudies",
      "BB@tv-basicstudies",
      "Stochastic@tv-basicstudies"
    ],
    container_id: "tv"
  }});
</script>
</body></html>
"""


# TradingView's tv.js widget accepts these interval codes: 1, 5, 15, 30,
# 60 (=H1), 240 (=H4), D, W.  We use M15 / H1 / H4 as required.
TF_DEFS: list[tuple[str, str, str]] = [
    ("15", "M15", "15 минут"),
    ("60", "H1", "1 час"),
    ("240", "H4", "4 часа"),
]


def capture_tradingview(pair: str) -> list[Path]:
    """Use Playwright to screenshot the TradingView widget on 3 TFs.

    Returns the list of generated PNG paths (in M15→H1→H4 order).
    Empty list if Playwright is unavailable or every shot failed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[top1_ai] playwright not installed — skipping screenshots",
              file=sys.stderr)
        return []

    out_paths: list[Path] = []
    tmp_dir = SCREENSHOTS / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Always render with the standard FX_IDC symbol like tv_screenshot.py does.
    tv_symbol = f"FX_IDC:{pair}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )
        page = context.new_page()
        try:
            for interval, tf_name, _ in TF_DEFS:
                html = TV_WIDGET_HTML.format(symbol=tv_symbol, interval=interval)
                html_path = tmp_dir / f"{pair}_{tf_name}.html"
                html_path.write_text(html, encoding="utf-8")
                try:
                    page.goto(html_path.as_uri(),
                              wait_until="domcontentloaded", timeout=30_000)
                    # Give TradingView's iframe time to render the chart +
                    # all four studies.  ``networkidle`` is unreliable on
                    # this widget so we just wait a generous fixed slot.
                    try:
                        page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                    page.wait_for_timeout(7_000)
                    target = SCREENSHOTS / f"{pair}_{tf_name}.png"
                    page.screenshot(path=str(target), full_page=False)
                    print(f"[top1_ai] saved {target}")
                    out_paths.append(target)
                except Exception as e:
                    print(f"[top1_ai] screenshot {tf_name} failed: {e}",
                          file=sys.stderr)
        finally:
            context.close()
            browser.close()
    return out_paths


# ── Headline text for Telegram ────────────────────────────────────────


def build_telegram_text(payload: dict, analysis: str, model_label: str) -> str:
    top1 = payload.get("top1") or payload.get("leading_candidate") or {}
    pair = top1.get("pair", "—")
    name_ru = top1.get("name_ru", pair)
    side = top1.get("side")
    conf = top1.get("confidence")
    tier = _tier_label(top1.get("tier"))
    fav = payload.get("favorite_check") or {}
    window = _compute_entry_window(payload)

    timing_block = "\n".join(_timing_lines(window))
    header = (
        f"{_banner(window)}\n"
        f"🤖 <b>ТОП-1 AI-АНАЛИЗ</b> (бинарный опцион, 5 ч)\n"
        f"{timing_block}\n"
        f"Пара: <b>{pair}</b> ({name_ru})\n"
        f"Сигнал: <b>{_side_ru(side)}</b> · "
        f"Уверенность: <b>{_fmt_pct(conf)}</b> · "
        f"Tier: <b>{tier}</b>\n"
        f"Gate ok: {fav.get('ok')} · floor: {fav.get('confidence_floor')}%\n"
        f"\n"
    )

    footer = f"\n\n— модель: {model_label}"
    # Telegram limit is 4096 chars for sendMessage; reserve room for footer.
    budget = 4000 - len(header) - len(footer)
    body = analysis.strip()
    if len(body) > budget:
        body = body[:budget - 1] + "…"
    return header + body + footer


# ── main ──────────────────────────────────────────────────────────────


def main() -> int:
    print("[top1_ai] running select_top1() — full 28-pair AI brain sweep…")
    from app.brain import select_top1  # local import keeps top fast

    payload = select_top1()
    top1 = payload.get("top1") or payload.get("leading_candidate")
    if not top1 or not top1.get("pair"):
        msg = ("⚠️ Brain не выбрал ни одной пары и leading_candidate пуст — "
               "пропускаем цикл AI-анализа.")
        print(msg)
        OUT_REPORT.write_text(f"# {msg}\n", encoding="utf-8")
        return 0

    prompt = build_prompt(payload)
    print(f"[top1_ai] prompt size: {len(prompt)} chars")

    analysis, model_label = generate_ai_analysis(prompt)
    print(f"[top1_ai] model used: {model_label} · analysis: {len(analysis)} chars")

    pair = top1.get("pair", "EURUSD")
    screenshots = capture_tradingview(pair)

    scanner_text = build_scanner_message(payload)
    tg_text = build_telegram_text(payload, analysis, model_label)

    OUT_REPORT.write_text(
        f"# ТОП-1 AI-анализ — {payload.get('generated_at_utc', '')}\n\n"
        f"## Сканер 28 пар\n\n```\n{scanner_text}\n```\n\n"
        f"## Подробный AI-анализ Top-1\n\n```\n{tg_text}\n```\n\n"
        f"## Полный prompt (для отладки)\n\n```\n{prompt}\n```\n\n"
        f"## Сырое тело LLM\n\n{analysis}\n",
        encoding="utf-8",
    )

    bot = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (bot and chat):
        print("[top1_ai] TELEGRAM_BOT_TOKEN/CHAT_ID missing — report saved, "
              "Telegram skipped")
        return 0

    ok_scan = telegram_send_message(bot, chat, scanner_text)
    print(f"[top1_ai] sendMessage scanner ok={ok_scan}")
    ok_text = telegram_send_message(bot, chat, tg_text)
    print(f"[top1_ai] sendMessage analysis ok={ok_text}")

    if screenshots:
        captions = [
            f"📊 {pair} M15 · RSI/MACD/BB/Stoch",
            f"📊 {pair} H1 · RSI/MACD/BB/Stoch",
            f"📊 {pair} H4 · RSI/MACD/BB/Stoch",
        ]
        ok_media = telegram_send_media_group(bot, chat, screenshots, captions)
        print(f"[top1_ai] sendMediaGroup ok={ok_media}")
    else:
        print("[top1_ai] no screenshots — sending text only")

    return 0


if __name__ == "__main__":
    sys.exit(main())
