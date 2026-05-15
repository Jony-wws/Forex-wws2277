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
Тебе дают полную информацию о ТОП-1 валютной паре из 28, выбранной AI-системой.

ВАЖНО: Это БИНАРНЫЕ ОПЦИОНЫ — нет Take Profit, нет Stop Loss, только время экспирации (5 часов).
Твоя задача — проанализировать все данные и дать прогноз BUY/SELL на 5 часов.

Требования к анализу:
1. Начни с чёткого прогноза: ПОКУПКА или ПРОДАЖА с уверенностью
2. Объясни ПОЧЕМУ выбрана именно эта пара (анализ 7 слоёв)
3. Опиши техническую картину (мульти-TF, ADX, persistence, индикаторы)
4. Объясни макро-контекст (DXY, ставки, сырье)
5. Оцени риски (новости, геополитика, волатильность)
6. Дай прогноз на 5 часов: куда пойдёт цена и почему
7. Пиши на русском языке, НЕ как шаблон, а как настоящий аналитик
8. Используй профессиональную терминологию, но понятно
9. Будь честным — если сигнал слабый, скажи об этом

Формат: структурированный анализ с эмодзи, 1500-2000 символов.
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

    lines: list[str] = []
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
    next_cycle = payload.get("next_cycle_utc") or payload.get("cycle_close_utc")
    minutes_to_expiry = payload.get("minutes_to_expiry")
    fav = payload.get("favorite_check") or {}

    header = (
        f"🤖 <b>ТОП-1 AI-АНАЛИЗ</b> (бинарный опцион, 5 ч)\n"
        f"Пара: <b>{pair}</b> ({name_ru})\n"
        f"Сигнал: <b>{_side_ru(side)}</b> · "
        f"Уверенность: <b>{_fmt_pct(conf)}</b> · "
        f"Tier: <b>{tier}</b>\n"
        f"Экспирация: {_utc_plus_5(next_cycle)} "
        f"(осталось ~{int(round(minutes_to_expiry or 0))} мин)\n"
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

    tg_text = build_telegram_text(payload, analysis, model_label)

    OUT_REPORT.write_text(
        f"# ТОП-1 AI-анализ — {payload.get('generated_at_utc', '')}\n\n"
        f"## Сводка\n\n```\n{tg_text}\n```\n\n"
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

    ok_text = telegram_send_message(bot, chat, tg_text)
    print(f"[top1_ai] sendMessage ok={ok_text}")

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
