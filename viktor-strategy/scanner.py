#!/usr/bin/env python3
"""
JONY EUR/USD 24/5 TREND FORECAST scanner (rebuilt 2026-06-10 per JONY).

New behaviour (JONY's framework): we do NOT wait for a rare signal. Every 30 min
during forex hours we ASSESS what's happening and send a REAL 5h forecast:
  - TREND from M15/H1/H4/D1 technicals (the anchor — trade only WITH trend)
  - smart-money liquidity levels (recent swing hi/lo)
  - fundamentals/news check (high-impact USD/EUR events within next 5h)
  - direction is ALWAYS with the dominant trend
  - ENTRY-QUALITY tag (🟢/🟡/🔴) so JONY enters on pullbacks, not chases / not flat

Time shown in Dushanbe/Tashkent (UTC+5). Honest confidence ~48-58%, never a guarantee.
"""
import os, json, asyncio, time
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(HERE, "config.json")))
STATE_PATH = os.path.join(HERE, "state.json")
TOKEN = CONFIG["telegram_token"]
CHAT_ID = CONFIG["chat_id"]
SYMBOL = CONFIG.get("symbol", "EURUSD=X")

# ---------- sessions (trained on 365 days of EUR/USD H1, see sessions.json) ----------
def load_sessions():
    p = os.path.join(HERE, "sessions.json")
    try:
        return json.load(open(p))
    except Exception:
        return None

SESS_STATS = load_sessions()

def current_session(now):
    """Return (key, stats) for the session the UTC hour falls into."""
    if not SESS_STATS:
        return None, None
    h = now.hour
    for key, s in SESS_STATS["sessions"].items():
        a, b = s["utc_hours"]
        if a <= h < b:
            return key, s
    return None, None

SESS_NOTES = {  # plain-Russian behaviour notes from the 365-day study
    "asia": "Азия — спокойная сессия, тренд дня соблюдает 50/50. Движения мелкие, часто пила.",
    "london": "⚠️ Лондон на открытии ЧАСТО идёт ПРОТИВ дневного тренда (по нашей статистике 365 дней — лишь 46% по тренду): крупные сначала снимают ликвидность за уровнями Азии. Входить по тренду тут опаснее обычного.",
    "overlap": "Лондон+Нью-Йорк — самые сильные движения суток (в среднем ~40 пипсов за сессию). Тренд соблюдается ~51% — лучшая сессия, чтобы цена реально ДОШЛА до цели.",
    "ny": "Нью-Йорк (вторая половина) — движение затухает после 21:00 по Ташкенту, тренд ~51%.",
    "late": "Тихие часы — движения маленькие (≈4 пипса), зато направление дня держится лучше всего (59% за 365 дней). Хорошо для аккуратного входа по тренду, но цели близкие.",
}

# ---------- data ----------
def fetch_ohlc(interval, rng):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
    params = {"interval": interval, "range": rng}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "time": pd.to_datetime(ts, unit="s", utc=True),
        "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
    }).dropna().reset_index(drop=True)
    return df

def resample_4h(df1h):
    d = df1h.set_index("time")
    o = d["open"].resample("4h").first()
    h = d["high"].resample("4h").max()
    l = d["low"].resample("4h").min()
    c = d["close"].resample("4h").last()
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}).dropna().reset_index()

# ---------- indicators ----------
def rsi(series, n=14):
    delta = series.diff()
    up = delta.clip(lower=0); dn = -delta.clip(upper=0)
    ru = up.ewm(alpha=1/n, adjust=False).mean()
    rd = dn.ewm(alpha=1/n, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    return 100 - 100/(1+rs)

def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()

def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h-l).abs(), (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def trend_state(df, fast=50, slow=200):
    if df is None or len(df) < slow:
        # fallback to shorter EMAs if not enough bars
        if df is None or len(df) < 60:
            return "flat"
        fast, slow = 20, 50
    ef = ema(df["close"], fast); es = ema(df["close"], slow)
    price = float(df["close"].iloc[-1])
    f, s = float(ef.iloc[-1]), float(es.iloc[-1])
    if price > s and f >= s:
        return "up"
    if price < s and f <= s:
        return "down"
    return "flat"

# ---------- forex hours ----------
def forex_open(now):
    wd = now.weekday(); h = now.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= 22
    if wd == 4:
        return h < 22
    return True

# ---------- news / fundamentals (NEWS FILTER, added 2026-06-11 per JONY) ----------
# Rule: if a high-impact USD/EUR event falls inside the 5h trade window, the
# scanner does NOT give a forecast. Instead it sends a news brief: what the
# event is, who speaks, when (UTC+5), and when it's SAFE to enter again.

NEWS_EXPLAIN = [
    ("CPI", "Индекс потребительских цен (инфляция). Выше прогноза → валюта обычно растёт (рынок ждёт жёсткую ставку), ниже → падает."),
    ("PPI", "Цены производителей — предвестник инфляции. Сюрприз двигает валюту как мини-CPI."),
    ("Non-Farm", "Занятость вне с/х США (NFP) — сильнейшая новость месяца, движения 50-100+ пипсов за минуты."),
    ("Federal Funds Rate", "Решение ФРС по процентной ставке — крупнейшее событие для USD. Реакция = сюрприз + тон Пауэлла."),
    ("FOMC", "ФРС (Федрезерв США): заявление/протокол/прогнозы по ставке. Рынок реагирует на ТОН, не только цифру."),
    ("Main Refinancing Rate", "Решение ЕЦБ по ставке — крупнейшее событие для EUR. Само решение часто в цене, решает тон Лагард."),
    ("Press Conference", "Пресс-конференция главы ЦБ — рынок торгует ТОН выступления; движения резкие и непредсказуемые."),
    ("GDP", "ВВП — рост экономики. Сильный сюрприз двигает валюту."),
    ("PMI", "Индекс деловой активности — опережающий индикатор экономики."),
    ("Retail Sales", "Розничные продажи — здоровье потребительского спроса."),
    ("Unemployment Claims", "Недельные заявки на пособие по безработице — индикатор рынка труда США."),
    ("Consumer Sentiment", "Настроения потребителей (Мичиган) — ожидания по расходам и инфляции."),
    ("Testifies", "Выступление перед парламентом/конгрессом — важен тон: жёстко → валюта растёт, мягко → падает."),
    ("Speaks", "Публичное выступление — важен тон: намёки на ставку двигают рынок в любую сторону."),
]

def explain_event(title):
    for key, txt in NEWS_EXPLAIN:
        if key.lower() in title.lower():
            return txt
    return "Важное макро-событие — возможна высокая волатильность, исход заранее неизвестен."

def event_speaker(title):
    """Extract who speaks, e.g. 'Fed Chair Powell Speaks' -> 'Fed Chair Powell'."""
    for suffix in (" Speaks", " Speech", " Testifies"):
        if suffix.lower() in title.lower():
            idx = title.lower().find(suffix.lower())
            return title[:idx].strip()
    if "Press Conference" in title:
        if "ECB" in title:
            return "Кристин Лагард (глава ЕЦБ)"
        if "FOMC" in title or "Fed" in title:
            return "Джером Пауэлл (глава ФРС)"
    return None

def event_buffer_min(title):
    """How long after the event the market needs to settle (safe re-entry buffer)."""
    t = title.lower()
    if any(k in t for k in ("funds rate", "refinancing rate", "fomc", "press conference")):
        return 120  # rate decisions + pressers: 2h
    if any(k in t for k in ("speaks", "speech", "testifies")):
        return 60
    if "non-farm" in t or "cpi" in t:
        return 90  # biggest data releases
    return 60

def fetch_news_risk(now, window_min=300, lookback_min=180):
    """High-impact USD/EUR events around now (Forex Factory free JSON).

    Returns dict:
      blocking: events that make entry UNSAFE (upcoming inside 5h window, or
                already released but still inside their settle buffer)
      recent:   released >buffer but <3h ago — market may still be digesting
      safe_after: UTC datetime when entry becomes safe again (or None)
    Returns None if the calendar couldn't be fetched.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cache_path = os.path.join(HERE, "news_cache.json")
    try:
        data = None
        for attempt in range(3):  # endpoint rate-limits (429); retry with backoff
            try:
                r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                r.raise_for_status()
                data = r.json()
                json.dump({"ts": now.isoformat(), "data": data}, open(cache_path, "w"))
                break
            except Exception as e:
                print(f"news fetch attempt {attempt+1} failed:", e)
                time.sleep(10 * (attempt + 1))
        if data is None and os.path.exists(cache_path):
            # fall back to cached weekly calendar (< 12h old)
            try:
                c = json.load(open(cache_path))
                age_h = (now - datetime.fromisoformat(c["ts"])).total_seconds() / 3600
                if age_h < 12:
                    data = c["data"]
                    print(f"using cached calendar ({age_h:.1f}h old)")
            except Exception as e:
                print("cache read err", e)
        if data is None:
            return None
        blocking, recent = [], []
        for ev in data:
            if ev.get("impact") != "High":
                continue
            if ev.get("country") not in ("USD", "EUR"):
                continue
            try:
                dt = datetime.fromisoformat(str(ev["date"]).replace("Z", "+00:00"))
                dt = dt.astimezone(timezone.utc)
            except Exception:
                continue
            mins = (dt - now).total_seconds() / 60.0
            buf = event_buffer_min(ev.get("title", ""))
            item = {
                "title": ev.get("title", "?"), "cur": ev.get("country"),
                "dt": dt, "tk": (dt + timedelta(hours=5)).strftime("%H:%M"),
                "mins": int(mins), "buffer": buf,
                "safe_dt": dt + timedelta(minutes=buf),
                "speaker": event_speaker(ev.get("title", "")),
                "explain": explain_event(ev.get("title", "")),
                "forecast": str(ev.get("forecast") or "").strip(),
                "previous": str(ev.get("previous") or "").strip(),
            }
            if 0 <= mins <= window_min:
                blocking.append(item)          # upcoming inside trade window
            elif -buf <= mins < 0:
                blocking.append(item)          # just released, still settling
            elif -lookback_min <= mins < -buf:
                recent.append(item)            # released, mostly digested
        blocking.sort(key=lambda x: x["mins"])
        recent.sort(key=lambda x: x["mins"])
        safe_after = max((e["safe_dt"] for e in blocking), default=None)
        return {"blocking": blocking, "recent": recent, "safe_after": safe_after}
    except Exception as e:
        print("news fetch err", e)
        return None  # None = couldn't check

def send_news_block(news, now):
    """Send the NO-FORECAST news brief to Telegram instead of a forecast."""
    now_tk = now + timedelta(hours=5)
    lines = [
        "🚫 *ПРОГНОЗ НЕ ВЫДАН — впереди важные новости*",
        f"🕐 {now_tk.strftime('%H:%M')} Душанбе (UTC+5)",
        "",
        "В 5-часовом окне сделки есть новости, исход которых заранее неизвестен. "
        "Входить сейчас ОПАСНО — график решать не будет, решат новости.",
        "",
    ]
    for i, ev in enumerate(news["blocking"], 1):
        flag = "🇺🇸" if ev["cur"] == "USD" else "🇪🇺"
        when = (f"в {ev['tk']} (через {ev['mins']}м)" if ev["mins"] >= 0
                else f"вышла в {ev['tk']} ({-ev['mins']}м назад, рынок ещё трясёт)")
        lines.append(f"📰 *{i}. {flag} {ev['cur']} — {ev['title']}*")
        lines.append(f"   🕐 {when}")
        if ev["speaker"]:
            lines.append(f"   👤 Выступает: {ev['speaker']}")
        if ev["forecast"] or ev["previous"]:
            fp = []
            if ev["forecast"]:
                fp.append(f"прогноз {ev['forecast']}")
            if ev["previous"]:
                fp.append(f"пред. {ev['previous']}")
            lines.append(f"   📊 {' · '.join(fp)}")
        lines.append(f"   ℹ️ {ev['explain']}")
        lines.append("")
    if news["safe_after"]:
        safe_tk = news["safe_after"] + timedelta(hours=5)
        day = "" if safe_tk.date() == now_tk.date() else f" ({safe_tk.strftime('%d.%m')})"
        lines.append(f"✅ *Безопасно заходить: после {safe_tk.strftime('%H:%M')}{day} (UTC+5)*")
        lines.append("(последняя новость + время, чтобы рынок переварил реакцию)")
        lines.append("")
    lines.append("Следующий прогноз пришлю автоматически, когда окно будет чистым.")
    text = "\n".join(lines)
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text,
                            "parse_mode": "Markdown"}, timeout=30)
    return r.json()

# ---------- liquidity traps (smart money) ----------
def liquidity_traps(h1, d1, now):
    """Detect FRESH liquidity sweeps (false breaks) of prev day/week highs/lows.

    Backtested (EURUSD H1 ~17k bars): prev-WEEK-high false break -> SELL ~55%.
    Rule: never enter AGAINST a fresh sweep; aligned sweep = small confirmation.
    """
    out = {"pdh": None, "pdl": None, "pwh": None, "pwl": None, "sweeps": []}
    try:
        d = d1.copy()
        d["date"] = d["time"].dt.date
        today = now.date()
        prev_days = d[d["date"] < today]
        if len(prev_days):
            out["pdh"] = float(prev_days["high"].iloc[-1])
            out["pdl"] = float(prev_days["low"].iloc[-1])
        iso = d["time"].dt.isocalendar()
        d["wk"] = iso["year"].astype(int) * 100 + iso["week"].astype(int)
        cur_iso = now.isocalendar()
        cur_wk = cur_iso[0] * 100 + cur_iso[1]
        pw = d[d["wk"] < cur_wk]
        if len(pw):
            wd = pw[pw["wk"] == int(pw["wk"].iloc[-1])]
            out["pwh"] = float(wd["high"].max())
            out["pwl"] = float(wd["low"].min())
        recent = h1.tail(12)  # fresh = last ~12 H1 bars
        price = float(h1["close"].iloc[-1])

        def check(level, name, side, weight):
            if level is None:
                return
            if side == "high":
                swept = (recent["high"] > level) & (recent["close"] < level)
                if bool(swept.any()) and price < level:
                    out["sweeps"].append({"name": name, "level": level,
                                          "bias": "SELL", "w": weight})
            else:
                swept = (recent["low"] < level) & (recent["close"] > level)
                if bool(swept.any()) and price > level:
                    out["sweeps"].append({"name": name, "level": level,
                                          "bias": "BUY", "w": weight})

        check(out["pwh"], "хай прошлой НЕДЕЛИ", "high", 2)  # best backtested
        check(out["pwl"], "лоу прошлой НЕДЕЛИ", "low", 2)
        check(out["pdh"], "вчерашний хай", "high", 1)
        check(out["pdl"], "вчерашний лоу", "low", 1)
        out["sweeps"].sort(key=lambda s: -s["w"])
    except Exception as e:
        print("trap calc err", e)
    return out

# ---------- forecast (trend-based, every run) ----------
def forecast(h1, m15, h4, d1, news, traps=None, now=None):
    price = float(h1["close"].iloc[-1])
    m15_rsi = float(rsi(m15["close"]).iloc[-1]) if len(m15) > 20 else 50.0
    h1_rsi = float(rsi(h1["close"]).iloc[-1])
    h4_rsi = float(rsi(h4["close"]).iloc[-1]) if len(h4) > 20 else 50.0
    h1_atr = float(atr(h1).iloc[-1])

    d1_tr = trend_state(d1); h4_tr = trend_state(h4); h1_tr = trend_state(h1)

    def sc(t):
        return 1 if t == "up" else (-1 if t == "down" else 0)
    score = 2.0*sc(d1_tr) + 1.5*sc(h4_tr) + 1.0*sc(h1_tr)  # range -4.5..4.5

    # direction = WITH dominant trend
    if score > 0.3:
        direction = "BUY"
    elif score < -0.3:
        direction = "SELL"
    else:
        # flat / conflicting -> lean by H1 EMA50 slope, mark as no-trend
        slope = float(ema(h1["close"], 50).diff().iloc[-1])
        direction = "BUY" if slope >= 0 else "SELL"

    a = abs(score)
    if a >= 2.5:
        strength = "сильный"
    elif a >= 0.8:
        strength = "умеренный"
    else:
        strength = "флэт/нет тренда"

    # position in recent H1 range (smart-money liquidity context)
    lo20 = float(h1["low"].tail(20).min()); hi20 = float(h1["high"].tail(20).max())
    rng = max(hi20 - lo20, 1e-6)
    pos = (price - lo20) / rng  # 0 = at lows, 1 = at highs

    # ENTRY QUALITY — enter on a pullback WITH the trend, skip flat / chasing
    flat = strength.startswith("флэт")
    if flat:
        eq, eq_txt = "🔴", "флэт/нет тренда — лучше ПРОПУСТИТЬ или мин. размер"
    elif direction == "SELL":
        if pos >= 0.6 or m15_rsi >= 55:
            eq, eq_txt = "🟢", "цена на отскоке вверх — хороший момент ПРОДАВАТЬ по тренду"
        elif pos >= 0.4:
            eq, eq_txt = "🟡", "средний — можно, но лучше отскок повыше"
        else:
            eq, eq_txt = "🔴", "цена уже внизу — НЕ гнаться, жди отскок к сопротивлению"
    else:  # BUY
        if pos <= 0.4 or m15_rsi <= 45:
            eq, eq_txt = "🟢", "цена на откате вниз — хороший момент ПОКУПАТЬ по тренду"
        elif pos <= 0.6:
            eq, eq_txt = "🟡", "средний — можно, но лучше откат пониже"
        else:
            eq, eq_txt = "🔴", "цена уже вверху — НЕ гнаться, жди откат к поддержке"

    # liquidity traps (smart money): never go AGAINST a fresh sweep
    trap_txt = ""
    trap_adj = 0
    if traps and traps.get("sweeps"):
        s = traps["sweeps"][0]
        if s["bias"] != direction:
            eq, eq_txt = "🔴", (f"свежий съём ликвидности ({s['name']} "
                                f"{s['level']:.5f}) ПРОТИВ направления — ПРОПУСТИТЬ")
            trap_adj = -3
            trap_txt = (f"🪤 *Ловушка:* крупные сняли стопы за {s['name']} "
                        f"({s['level']:.5f}) — толкают ПРОТИВ тренда, не входить.")
        else:
            trap_adj = 2
            trap_txt = (f"🪤 *Ловушка в нашу пользу:* ложный пробой {s['name']} "
                        f"({s['level']:.5f}) → крупные набрали позицию в сторону {direction}.")
    elif traps:
        trap_txt = "🪤 Свежих съёмов ликвидности (день/неделя) нет — ловушек не видно."

    # session intelligence (trained on 365 days, sessions.json)
    sess_key, sess = current_session(now or datetime.now(timezone.utc))
    sess_adj = 0
    sess_txt = ""
    if sess:
        sess_adj = int(sess.get("conf_adj", 0))
        sess_txt = (f"🕐 *Сессия:* {sess['ru']} "
                    f"({sess['tk_hours'][0]:02d}:00–{sess['tk_hours'][1]:02d}:00 Ташкент)\n"
                    f"   {SESS_NOTES.get(sess_key, '')}")

    # confidence (honest)
    conf = 50.0
    conf += 4 if a >= 2.5 else (2 if a >= 0.8 else 0)
    conf += {"🟢": 3, "🟡": 1, "🔴": -2}[eq]
    conf += trap_adj
    conf += sess_adj
    news_warn = ""
    if news:  # list of upcoming high-impact events
        conf -= 3
        ev = news[0]
        news_warn = (f"⚠️ Скоро ВАЖНАЯ новость: {ev['cur']} {ev['title']} в "
                     f"{ev['tk']} (через {ev['mins']}м) — высокая волатильность.")
    elif news is None:
        conf -= 2
        news_warn = ("⚠️ НЕ СМОГ проверить календарь новостей! ПЕРЕД входом сам "
                     "проверь forexfactory.com — если в ближайшие 5ч есть красные "
                     "новости USD/EUR, НЕ заходи.")
    conf = int(max(48, min(58, conf)))

    # target / invalidation (5h horizon, with-trend)
    if direction == "SELL":
        target = round(price - 1.5*h1_atr, 5)
        invalid = round(max(hi20, price + 1.5*h1_atr), 5)
    else:
        target = round(price + 1.5*h1_atr, 5)
        invalid = round(min(lo20, price - 1.5*h1_atr), 5)

    return {
        "direction": direction, "price": round(price, 5),
        "target": target, "invalid": invalid, "conf": conf,
        "d1_tr": d1_tr, "h4_tr": h4_tr, "h1_tr": h1_tr,
        "strength": strength, "eq": eq, "eq_txt": eq_txt,
        "m15_rsi": round(m15_rsi, 0), "h1_rsi": round(h1_rsi, 0), "h4_rsi": round(h4_rsi, 0),
        "support": round(lo20, 5), "resistance": round(hi20, 5),
        "news_warn": news_warn, "flat": flat,
        "trap_txt": trap_txt,
        "sess_txt": sess_txt, "sess_key": sess_key, "sess_adj": sess_adj,
        "score": round(score, 1), "pos": round(pos, 2), "h1_atr": round(h1_atr, 5),
        "pwh": traps.get("pwh") if traps else None,
        "pwl": traps.get("pwl") if traps else None,
        "pdh": traps.get("pdh") if traps else None,
        "pdl": traps.get("pdl") if traps else None,
    }

# ---------- charts ----------
async def _capture_tv():
    from sdk.utils.browser import get_browser, close_browser
    b = await get_browser("tvscan", viewport_width=1600, viewport_height=900)
    shots = {"M15": "15", "H1": "60", "H4": "240", "D1": "D"}
    out = {}
    try:
        for name, iv in shots.items():
            await b.goto(f"https://www.tradingview.com/chart/?symbol=FX%3AEURUSD&interval={iv}",
                         timeout=60000)
            await asyncio.sleep(9)
            try:
                await b.press_key("Escape")
            except Exception:
                pass
            await asyncio.sleep(1)
            p = f"/tmp/jony_charts/tv_{name}.png"
            await b.take_screenshot(p)
            out[name] = p
    finally:
        try:
            await close_browser("tvscan")
        except Exception:
            pass
    return [out["M15"], out["H1"], out["H4"], out["D1"]]

def capture_charts(m15, h1, h4, d1=None):
    os.makedirs("/tmp/jony_charts", exist_ok=True)
    try:
        async def _runner():
            return await asyncio.wait_for(_capture_tv(), timeout=120)
        paths = asyncio.run(_runner())
        if all(os.path.exists(p) and os.path.getsize(p) > 30000 for p in paths):
            return paths
    except Exception as e:
        print("tv capture failed -> fallback:", e)
    tmp = "/tmp/jony_charts"
    p15, p1, p4 = f"{tmp}/m15.png", f"{tmp}/h1.png", f"{tmp}/h4.png"
    plot_candles(m15, "M15", p15)
    plot_candles(h1, "H1", p1)
    plot_candles(h4, "H4", p4)
    paths = [p15, p1, p4]
    if d1 is not None:
        pD = f"{tmp}/d1.png"
        plot_candles(d1, "D1", pD)
        paths.append(pD)
    return paths

def plot_candles(df, label, out_path, ema_fast=50, ema_slow=200, nbars=70):
    d = df.tail(nbars).reset_index(drop=True)
    closes = d["close"]
    efa = ema(df["close"], ema_fast).tail(nbars).reset_index(drop=True)
    esl = ema(df["close"], ema_slow).tail(nbars).reset_index(drop=True)
    rs = rsi(df["close"]).tail(nbars).reset_index(drop=True)
    fig, (ax, axr) = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1]})
    fig.patch.set_facecolor("#0e1117")
    for a in (ax, axr):
        a.set_facecolor("#0e1117")
        a.tick_params(colors="#aaaaaa", labelsize=8)
        for s in a.spines.values():
            s.set_color("#333333")
    w = 0.6
    for i, row in d.iterrows():
        up = row["close"] >= row["open"]
        col = "#26a69a" if up else "#ef5350"
        ax.plot([i, i], [row["low"], row["high"]], color=col, linewidth=0.8, zorder=1)
        lo = min(row["open"], row["close"]); hi = max(row["open"], row["close"])
        ax.add_patch(Rectangle((i-w/2, lo), w, max(hi-lo, 1e-6), color=col, zorder=2))
    ax.plot(range(len(efa)), efa, color="#f0b90b", linewidth=1.1, label=f"EMA{ema_fast}")
    ax.plot(range(len(esl)), esl, color="#29b6f6", linewidth=1.1, label=f"EMA{ema_slow}")
    ax.legend(loc="upper left", fontsize=7, facecolor="#0e1117",
              edgecolor="#333333", labelcolor="#cccccc")
    last_px = closes.iloc[-1]
    ax.axhline(last_px, color="#888888", linewidth=0.6, linestyle="--")
    ax.set_title(f"EUR/USD  {label}    {last_px:.5f}", color="#ffffff",
                 fontsize=11, fontweight="bold", loc="left")
    ax.margins(x=0.01)
    axr.plot(range(len(rs)), rs, color="#ab47bc", linewidth=1.0)
    axr.axhline(70, color="#ef5350", linewidth=0.6, linestyle="--")
    axr.axhline(30, color="#26a69a", linewidth=0.6, linestyle="--")
    axr.set_ylim(0, 100)
    axr.text(0.5, 78, f"RSI {rs.iloc[-1]:.0f}", color="#ab47bc", fontsize=8)
    axr.set_xticks([])
    plt.tight_layout()
    fig.savefig(out_path, dpi=110, facecolor="#0e1117")
    plt.close(fig)

# ---------- telegram ----------
TREND_RU = {"up": "⬆️вверх", "down": "⬇️вниз", "flat": "↔️флэт"}

def send_forecast(f, img_paths):
    arrow = "🟢⬆️ ПОКУПКА (BUY)" if f["direction"] == "BUY" else "🔴⬇️ ПРОДАЖА (SELL)"
    now_tk = datetime.now(timezone.utc) + timedelta(hours=5)
    dom = ("НИСХОДЯЩИЙ ⬇️" if f["direction"] == "SELL" else "ВОСХОДЯЩИЙ ⬆️")
    if f["flat"]:
        dom = "НЕТ ТРЕНДА ↔️"
    caption = (
        f"📡 *EUR/USD — ПРОГНОЗ на 5 часов*\n"
        f"🕐 {now_tk.strftime('%H:%M')} Душанбе (UTC+5)\n\n"
        f"📈 *Тренд:* D1 {TREND_RU[f['d1_tr']]} · H4 {TREND_RU[f['h4_tr']]} · "
        f"H1 {TREND_RU[f['h1_tr']]}\n"
        f"➡️ Доминирующий: *{dom}* ({f['strength']})\n\n"
        f"🎯 *Направление по тренду: {arrow}*\n"
        f"💵 Цена сейчас: *{f['price']:.5f}*\n"
        f"{f['eq']} *Момент входа:* {f['eq_txt']}\n"
        f"🎯 Цель (5ч): {f['target']:.5f}\n"
        f"🛑 Инвалидация: {f['invalid']:.5f}\n"
        f"📊 Уверенность: *{f['conf']}%*\n\n"
        f"🧠 *Smart money:* поддержка {f['support']:.5f} / "
        f"сопротивление {f['resistance']:.5f}\n"
        + (f"📅 Уровни ликвидности: нед. {f['pwl']:.5f}-{f['pwh']:.5f} · "
           f"день {f['pdl']:.5f}-{f['pdh']:.5f}\n"
           if f.get("pwh") and f.get("pdh") else "")
        + (f"{f['trap_txt']}\n" if f.get("trap_txt") else "")
        + f"📉 RSI: M15 {f['m15_rsi']:.0f} · H1 {f['h1_rsi']:.0f} · H4 {f['h4_rsi']:.0f}\n"
        f"{f['news_warn']}\n\n"
        f"⚠️ Не гарантия (~{f['conf']}%). Вход только ПО тренду, на откате. "
        f"Риск ≤1-2% депозита.\n"
        f"👇 Подробный разбор простыми словами — следующим сообщением."
    )
    media, files = [], {}
    for i, p in enumerate(img_paths):
        key = f"photo{i}"
        files[key] = open(p, "rb")
        item = {"type": "photo", "media": f"attach://{key}"}
        if i == 0:
            item["caption"] = caption
            item["parse_mode"] = "Markdown"
        media.append(item)
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMediaGroup",
                      data={"chat_id": CHAT_ID, "media": json.dumps(media)},
                      files=files, timeout=60)
    for fp in files.values():
        fp.close()
    return r.json()

def send_explainer(f):
    """Second message: every factor of the forecast explained in plain Russian."""
    act = "ПРОДАВАТЬ (ставить на ПАДЕНИЕ ⬇️)" if f["direction"] == "SELL" else \
          "ПОКУПАТЬ (ставить на РОСТ ⬆️)"
    eq_mean = {
        "🟢": "🟢 = момент входа ХОРОШИЙ, можно открывать сделку сейчас.",
        "🟡": "🟡 = момент средний: войти можно, но лучше дождаться цены получше.",
        "🔴": "🔴 = сейчас НЕ ВХОДИТЬ. Это не значит «прогноз неверный» — это значит "
              "«не открывай сделку в эту минуту», жди следующий прогноз или лучшую цену.",
    }[f["eq"]]
    pos_pct = int(f.get("pos", 0.5) * 100)
    lines = [
        "📖 *РАЗБОР ПРОГНОЗА ПРОСТЫМИ СЛОВАМИ*",
        "",
        f"1️⃣ *Куда смотрим:* рынок в целом идёт "
        f"{'ВНИЗ' if f['direction']=='SELL' else 'ВВЕРХ'} "
        f"(дневной график D1: {TREND_RU[f['d1_tr']]}, 4-часовой H4: {TREND_RU[f['h4_tr']]}, "
        f"часовой H1: {TREND_RU[f['h1_tr']]}; суммарный балл тренда {f['score']:+.1f} из ±4.5). "
        f"Поэтому если торговать — то {act}.",
        "",
        f"2️⃣ *Можно ли входить ПРЯМО СЕЙЧАС:* {eq_mean}",
        f"   Почему: {f['eq_txt']}. Цена сейчас на {pos_pct}% высоты последнего диапазона "
        f"(0% = у низа, 100% = у верха).",
        "",
    ]
    if f.get("trap_txt"):
        lines += [f"3️⃣ *Ловушки крупных игроков:* {f['trap_txt']}",
                  "   «Съём ликвидности» = цена ложно проколола важный уровень (хай/лоу дня "
                  "или недели), собрала чужие стопы и вернулась. Сразу после такого прокола "
                  "крупные часто толкают цену в ОБРАТНУЮ сторону — поэтому против свежего "
                  "съёма мы не входим.", ""]
    if f.get("sess_txt"):
        lines += [f"4️⃣ {f['sess_txt']}", ""]
    lines += [
        f"5️⃣ *План сделки:* вход {f['price']:.5f} → цель {f['target']:.5f} "
        f"(примерно {abs(f['target']-f['price'])/0.0001:.0f} пипсов, это 1.5×ATR — "
        f"средний ход цены за час × 1.5). Если цена уйдёт за {f['invalid']:.5f} — "
        f"прогноз сломан, не пересиживать.",
        "",
        f"6️⃣ *Новости:* {f['news_warn'] or 'окно чистое.'}",
        "",
        f"7️⃣ *Уверенность {f['conf']}%* — это честная оценка: даже хороший сетап на "
        f"5 часов угадывается чуть лучше монетки. Поэтому риск на сделку ≤1-2% депозита, "
        f"всегда.",
    ]
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": "\n".join(lines),
                            "parse_mode": "Markdown"}, timeout=30)
    return r.json()

# ---------- state ----------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH))
        except Exception:
            pass
    return {"day": "", "count": 0}

def save_state(s):
    json.dump(s, open(STATE_PATH, "w"), indent=2)

# ---------- main ----------
def main():
    now = datetime.now(timezone.utc)
    if not forex_open(now):
        print("forex closed")
        return
    try:
        m15 = fetch_ohlc("15m", "5d")
        h1 = fetch_ohlc("60m", "60d")
        d1 = fetch_ohlc("1d", "2y")
    except Exception as e:
        print("fetch error", e)
        return
    h4 = resample_4h(h1)

    news = fetch_news_risk(now)

    # NEWS FILTER (per JONY 2026-06-11): unsafe window -> news brief, NO forecast
    if news and news["blocking"]:
        resp = send_news_block(news, now)
        print("news-block", resp.get("ok"),
              [e["title"] for e in news["blocking"]],
              "safe_after", news["safe_after"])
        return

    traps = liquidity_traps(h1, d1, now)
    f = forecast(h1, m15, h4, d1, None if news is None else [], traps, now=now)

    # already-released news (>buffer, <3h ago): allow entry but warn + lower conf
    if news and news["recent"]:
        ev = news["recent"][-1]  # most recent
        f["news_warn"] = (f"📰 Уже вышла новость: {ev['cur']} {ev['title']} в {ev['tk']} "
                          f"({-ev['mins']}м назад) — рынок может ещё отыгрывать её. "
                          + (f.get("news_warn") or ""))
        f["conf"] = max(48, f["conf"] - 2)
    elif news is not None:
        f["news_warn"] = "📰 Важных новостей (USD/EUR) в ближайшие 5ч нет — окно чистое."

    charts = capture_charts(m15, h1, h4, d1)
    resp = send_forecast(f, charts)
    ok = resp.get("ok")
    if ok:
        try:
            send_explainer(f)
        except Exception as e:
            print("explainer err", e)
    print("sent", ok, f["direction"], f["conf"], f["eq"], "sess", f.get("sess_key"))

    state = load_state()
    today = now.strftime("%Y-%m-%d")
    if state.get("day") != today:
        state = {"day": today, "count": 0}
    if ok:
        state["count"] = state.get("count", 0) + 1
        save_state(state)

if __name__ == "__main__":
    main()
