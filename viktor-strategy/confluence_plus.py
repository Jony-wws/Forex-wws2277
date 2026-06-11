#!/usr/bin/env python3
"""
CONFLUENCE+ — дополнительные «планы» уровня крупных игроков (добавлено 13.06.2026
по голосовому JONY: «такие же планы, аналогичные, которые я ещё не знаю»).

Каждый модуль — независимый источник информации о том, куда пойдёт EUR/USD.
Они НЕ дублируют smart_money.py (структура графика) — они смотрят на рынок
с других сторон:

  1. COT (CFTC) — РЕАЛЬНЫЕ позиции крупных спекулянтов на фьючерсе EUR.
     Единственные публичные данные, где видно, куда крупные УЖЕ вложили деньги.
     Публикуется каждую пятницу. Берём z-score за 52 недели: экстремально
     длинные → бычий EUR, экстремально короткие → медвежий.
  2. DXY (индекс доллара) — EUR/USD на ~57% состоит из EUR против USD.
     Если DXY падает → EUR/USD растёт. Дивергенция (EUR/USD делает новый лоу,
     а DXY НЕ делает новый хай) = скрытая сила евро.
  3. Доходность US10Y (^TNX) — деньги текут туда, где платят больше.
     Резкий рост доходности США → доллар сильнее → EUR/USD вниз.
  4. RSI-ЭКСТРЕМУМ (наш ЕДИНСТВЕННЫЙ бэктест-подтверждённый эдж: 54-58% на
     17 000 баров) — H1 RSI <= 25 → перепроданность, отскок ВВЕРХ;
     RSI >= 75 → перекупленность, откат ВНИЗ.
  5. КРУГЛЫЕ УРОВНИ (00/50) — психология толпы + опционные барьеры. Отбой от
     круглого уровня в нашу сторону = подтверждение; круглый уровень стеной
     прямо на пути к цели = предупреждение.
  6. СВЕЧНОЕ ПОДТВЕРЖДЕНИЕ — закрытое поглощение/пин-бар H1 в сторону прогноза.

Каждый модуль голосует [-2..+2]. Сумма = cp_score. Никогда не роняет сканер:
любая ошибка сети/данных = голос 0 + честная пометка в lines.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PIP = 0.0001

# ---------------------------------------------------------------- COT (CFTC)
COT_ENDPOINT = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
COT_MARKET = "EURO FX - CHICAGO MERCANTILE EXCHANGE"
COT_CACHE = os.path.join(HERE, "cot_cache.json")
COT_TTL_H = 12  # отчёт недельный, 12ч кэша достаточно


def cot_eur_vote():
    """(vote, line). Позиции крупных спекулянтов во фьючерсе EUR (z-score 52w)."""
    try:
        data = None
        if os.path.exists(COT_CACHE):
            try:
                c = json.load(open(COT_CACHE))
                if time.time() - c["ts"] < COT_TTL_H * 3600:
                    data = c["rows"]
            except Exception:
                pass
        if data is None:
            qs = urllib.parse.urlencode({
                "$where": f"market_and_exchange_names = '{COT_MARKET}'",
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": "52",
                "$select": ("report_date_as_yyyy_mm_dd,"
                            "noncomm_positions_long_all,noncomm_positions_short_all"),
            })
            req = urllib.request.Request(f"{COT_ENDPOINT}?{qs}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            json.dump({"ts": time.time(), "rows": data}, open(COT_CACHE, "w"))
        if not data or len(data) < 10:
            return 0, "🏦 COT: данных CFTC мало — голос 0."
        nets = [float(r["noncomm_positions_long_all"]) -
                float(r["noncomm_positions_short_all"]) for r in data]
        cur = nets[0]
        mu = float(np.mean(nets))
        sd = float(np.std(nets)) or 1.0
        z = (cur - mu) / sd
        vote = float(max(-2.0, min(2.0, z)))
        side = "ЛОНГ (ставят на рост EUR)" if cur > 0 else "ШОРТ (ставят на падение EUR)"
        trend_note = ""
        if z >= 0.7:
            trend_note = " — позиция НАРАЩИВАЕТСЯ против года"
        elif z <= -0.7:
            trend_note = " — но СОКРАЩАЮТ её против среднего за год"
        date = data[0].get("report_date_as_yyyy_mm_dd", "")[:10]
        return round(vote, 1), (f"🏦 COT (CFTC, {date}): крупные спекулянты net-{side}, "
                                f"{cur:+,.0f} контрактов{trend_note}, z={z:+.1f} → голос {vote:+.1f}.")
    except Exception as e:
        return 0, f"🏦 COT: не смог получить данные CFTC ({type(e).__name__}) — голос 0."


# ----------------------------------------------------------------- DXY / TNX
def _yahoo(symbol, interval, rng):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"interval": interval, "range": rng},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "time": pd.to_datetime(res["timestamp"], unit="s", utc=True),
        "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
    }).dropna().reset_index(drop=True)
    return df


def dxy_vote(eur_h1):
    """(vote, line). Индекс доллара: тренд + дивергенция с EUR/USD."""
    try:
        dxy = None
        for sym in ("DX=F", "DX-Y.NYB"):
            try:
                dxy = _yahoo(sym, "60m", "10d")
                if len(dxy) > 30:
                    break
            except Exception:
                continue
        if dxy is None or len(dxy) < 30:
            return 0, "💵 DXY: котировки недоступны — голос 0."
        e20 = dxy["close"].ewm(span=20, adjust=False).mean()
        e50 = dxy["close"].ewm(span=50, adjust=False).mean()
        vote = 0.0
        if float(e20.iloc[-1]) < float(e50.iloc[-1]):
            vote += 1.0  # доллар слабеет → EUR/USD вверх
            t = "DXY слабеет (EMA20<EMA50) → попутный ветер для РОСТА EUR/USD"
        elif float(e20.iloc[-1]) > float(e50.iloc[-1]):
            vote -= 1.0
            t = "DXY укрепляется (EMA20>EMA50) → давление на EUR/USD ВНИЗ"
        else:
            t = "DXY во флэте"
        # дивергенция за последние 24 часа против предыдущих 24
        try:
            eur_new_low = float(eur_h1["low"].tail(24).min()) < float(eur_h1["low"].tail(48).head(24).min())
            eur_new_high = float(eur_h1["high"].tail(24).max()) > float(eur_h1["high"].tail(48).head(24).max())
            dxy_new_high = float(dxy["high"].tail(24).max()) > float(dxy["high"].tail(48).head(24).max())
            dxy_new_low = float(dxy["low"].tail(24).min()) < float(dxy["low"].tail(48).head(24).min())
            if eur_new_low and not dxy_new_high:
                vote += 1.0
                t += "; ДИВЕРГЕНЦИЯ: EUR/USD сделал новый лоу, а DXY новый хай НЕ сделал → скрытая сила евро"
            elif eur_new_high and not dxy_new_low:
                vote -= 1.0
                t += "; ДИВЕРГЕНЦИЯ: EUR/USD сделал новый хай, а DXY новый лоу НЕ сделал → скрытая слабость евро"
        except Exception:
            pass
        vote = max(-2.0, min(2.0, vote))
        return vote, f"💵 {t} → голос {vote:+.1f}."
    except Exception as e:
        return 0, f"💵 DXY: ошибка ({type(e).__name__}) — голос 0."


def us10y_vote():
    """(vote, line). Импульс доходности US10Y за ~5 дней."""
    try:
        tnx = _yahoo("^TNX", "1d", "1mo")
        if len(tnx) < 6:
            return 0, "📈 US10Y: мало данных — голос 0."
        cur = float(tnx["close"].iloc[-1])
        prev = float(tnx["close"].iloc[-6])
        if cur > 20:  # старый формат ^TNX = yield*10
            cur, prev = cur / 10, prev / 10
        chg = cur - prev
        if chg >= 0.10:        # >= +0.10% за ~неделю — заметный рост
            return -1.0, (f"📈 Доходность US10Y растёт ({prev:.2f}%→{cur:.2f}%) → "
                          f"деньги идут в доллар → давление на EUR/USD ВНИЗ → голос -1.")
        if chg <= -0.10:
            return 1.0, (f"📈 Доходность US10Y падает ({prev:.2f}%→{cur:.2f}%) → "
                         f"доллар теряет привлекательность → EUR/USD ВВЕРХ → голос +1.")
        return 0, f"📈 US10Y почти без изменений ({cur:.2f}%) — голос 0."
    except Exception as e:
        return 0, f"📈 US10Y: ошибка ({type(e).__name__}) — голос 0."


# ------------------------------------------------------------- RSI-экстремум
def rsi_extreme_vote(h1):
    """(vote, line, is_edge). НАШ лучший бэктест-эдж (54-58% на 17k баров)."""
    try:
        delta = h1["close"].diff()
        up = delta.clip(lower=0)
        dn = -delta.clip(upper=0)
        rs = (up.ewm(alpha=1 / 14, adjust=False).mean() /
              dn.ewm(alpha=1 / 14, adjust=False).mean().replace(0, np.nan))
        r = float((100 - 100 / (1 + rs)).iloc[-1])
        if r <= 25:
            return 2.0, (f"🎯 RSI-ЭКСТРЕМУМ (бэктест 54-58%!): H1 RSI={r:.0f} — сильная "
                         f"перепроданность → статистический отскок ВВЕРХ → голос +2."), True
        if r >= 75:
            return -2.0, (f"🎯 RSI-ЭКСТРЕМУМ (бэктест 54-58%!): H1 RSI={r:.0f} — сильная "
                          f"перекупленность → статистический откат ВНИЗ → голос -2."), True
        if r <= 32:
            return 1.0, f"🎯 RSI H1={r:.0f} — близко к перепроданности → голос +1.", False
        if r >= 68:
            return -1.0, f"🎯 RSI H1={r:.0f} — близко к перекупленности → голос -1.", False
        return 0, f"🎯 RSI H1={r:.0f} — нейтральная зона, эджа нет — голос 0.", False
    except Exception as e:
        return 0, f"🎯 RSI-экстремум: ошибка ({type(e).__name__}) — голос 0.", False


# ------------------------------------------------------------ круглые уровни
def round_level_vote(h1, direction):
    """(vote, line). Отбой от 00/50 в нашу сторону = +1; уровень стеной на пути = -0.5."""
    try:
        price = float(h1["close"].iloc[-1])
        step = 0.0050
        near = round(price / step) * step
        recent = h1.tail(8)
        vote, notes = 0.0, []
        for lvl in (near - step, near, near + step):
            touched_low = (recent["low"] <= lvl + 3 * PIP) & (recent["close"] > lvl + 3 * PIP)
            touched_high = (recent["high"] >= lvl - 3 * PIP) & (recent["close"] < lvl - 3 * PIP)
            if bool(touched_low.any()) and price > lvl and direction == "BUY":
                vote += 1.0
                notes.append(f"отбой ВВЕРХ от круглого {lvl:.4f} — поддержка работает")
            if bool(touched_high.any()) and price < lvl and direction == "SELL":
                vote += 1.0
                notes.append(f"отбой ВНИЗ от круглого {lvl:.4f} — сопротивление работает")
        ahead = near + step if direction == "BUY" else near - step
        dist = abs(ahead - price) / PIP
        if dist <= 12:
            vote -= 0.5
            notes.append(f"круглый {ahead:.4f} в {dist:.0f} пипсах ПО ПУТИ — может стать стеной")
        vote = max(-1.0, min(2.0, vote))
        if not notes:
            return 0, "⭕ Круглые уровни (00/50): рядом нет — голос 0."
        return vote, f"⭕ Круглые уровни: {'; '.join(notes)} → голос {vote:+.1f}."
    except Exception as e:
        return 0, f"⭕ Круглые уровни: ошибка ({type(e).__name__}) — голос 0."


# ------------------------------------------------------ свечное подтверждение
def candle_confirm_vote(h1, direction):
    """(vote, line). Закрытое поглощение / пин-бар H1 в сторону прогноза."""
    try:
        if len(h1) < 3:
            return 0, "🕯 Свечи: мало данных — голос 0."
        a, b = h1.iloc[-3], h1.iloc[-2]  # b = последняя ЗАКРЫТАЯ свеча
        body = abs(b["close"] - b["open"])
        rng = max(b["high"] - b["low"], 1e-9)
        bull_engulf = (b["close"] > b["open"] and a["close"] < a["open"]
                       and b["close"] >= a["open"] and b["open"] <= a["close"])
        bear_engulf = (b["close"] < b["open"] and a["close"] > a["open"]
                       and b["close"] <= a["open"] and b["open"] >= a["close"])
        low_wick = min(b["open"], b["close"]) - b["low"]
        high_wick = b["high"] - max(b["open"], b["close"])
        bull_pin = low_wick >= 2 * body and low_wick >= 0.6 * rng
        bear_pin = high_wick >= 2 * body and high_wick >= 0.6 * rng
        if direction == "BUY" and (bull_engulf or bull_pin):
            what = "бычье поглощение" if bull_engulf else "пин-бар с длинной нижней тенью"
            return 1.0, f"🕯 Свечное подтверждение: {what} на H1 — покупатели выкупают → голос +1."
        if direction == "SELL" and (bear_engulf or bear_pin):
            what = "медвежье поглощение" if bear_engulf else "пин-бар с длинной верхней тенью"
            return 1.0, f"🕯 Свечное подтверждение: {what} на H1 — продавцы продавили → голос +1."
        if direction == "BUY" and (bear_engulf or bear_pin):
            return -1.0, "🕯 Свечи ПРОТИВ: медвежья формация на H1 при прогнозе BUY → голос -1."
        if direction == "SELL" and (bull_engulf or bull_pin):
            return -1.0, "🕯 Свечи ПРОТИВ: бычья формация на H1 при прогнозе SELL → голос -1."
        return 0, "🕯 Свечи: явной формации на последней H1 нет — голос 0."
    except Exception as e:
        return 0, f"🕯 Свечи: ошибка ({type(e).__name__}) — голос 0."


# -------------------------------------------------------------------- сборка
def analyze_confluence_plus(h1, direction):
    """Все дополнительные «планы» одним вердиктом.

    direction — предварительное направление (от крупных игроков / тренда).
    Returns dict: score (сумма голосов, + = BUY, - = SELL), bias, lines,
    aligned (голоса ЗА направление), against (ПРОТИВ), rsi_edge (bool).
    """
    lines = []
    cot_v, cot_l = cot_eur_vote()
    dxy_v, dxy_l = dxy_vote(h1)
    tnx_v, tnx_l = us10y_vote()
    rsi_v, rsi_l, rsi_edge = rsi_extreme_vote(h1)
    rnd_v, rnd_l = round_level_vote(h1, direction)
    cnd_v, cnd_l = candle_confirm_vote(h1, direction)
    # rnd/cnd уже направленные (+ = за direction) → переводим в BUY/SELL знак
    sgn = 1 if direction == "BUY" else -1
    score = cot_v + dxy_v + tnx_v + rsi_v + sgn * rnd_v + sgn * cnd_v
    lines = [cot_l, dxy_l, tnx_l, rsi_l, rnd_l, cnd_l]
    bias = "BUY" if score >= 1.0 else ("SELL" if score <= -1.0 else None)
    dir_sgn = 1 if direction == "BUY" else -1
    directional = [cot_v, dxy_v, tnx_v, rsi_v, sgn * rnd_v * dir_sgn, sgn * cnd_v * dir_sgn]
    aligned = sum(1 for v in [cot_v, dxy_v, tnx_v, rsi_v] if v * dir_sgn >= 1.0)
    aligned += sum(1 for v in [rnd_v, cnd_v] if v >= 1.0)
    against = sum(1 for v in [cot_v, dxy_v, tnx_v, rsi_v] if v * dir_sgn <= -1.0)
    against += sum(1 for v in [rnd_v, cnd_v] if v <= -1.0)
    rsi_edge_aligned = rsi_edge and (rsi_v * dir_sgn > 0)
    return {"score": round(score, 1), "bias": bias, "lines": lines,
            "aligned": aligned, "against": against,
            "rsi_edge": rsi_edge_aligned,
            "votes": {"cot": cot_v, "dxy": dxy_v, "us10y": tnx_v,
                      "rsi": rsi_v, "round": rnd_v, "candle": cnd_v}}


if __name__ == "__main__":
    # самотест на живых данных
    h1 = _yahoo("EURUSD=X", "60m", "60d")
    for d in ("BUY", "SELL"):
        r = analyze_confluence_plus(h1, d)
        print(f"\n=== direction={d}: score={r['score']} bias={r['bias']} "
              f"aligned={r['aligned']} against={r['against']} rsi_edge={r['rsi_edge']}")
        for ln in r["lines"]:
            print(" ", ln)
