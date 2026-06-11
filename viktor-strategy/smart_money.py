#!/usr/bin/env python3
"""
SMART MONEY engine — ОСНОВА системы (добавлено 2026-06-12 по запросу JONY).

Иерархия анализа (зафиксирована):
  1. КРУПНЫЕ ИГРОКИ (этот модуль)  ← основа, главный голос
  2. Технический анализ (RSI, ATR, уровни)
  3. Тренд (EMA50/200 D1/H4/H1)
  4. Фундаментал  5. Новости  6. Макро  7. Прочие факторы

Модуль читает график как крупный игрок: НЕ «они собирают стопы» (это все знают),
а КУДА они поведут цену. Компоненты:

  A. СТРУКТУРА РЫНКА (H4 + H1): свинги HH/HL vs LH/LL, BOS (пробой структуры =
     продолжение), CHoCH (слом характера = разворот). Структура = след крупных:
     один игрок не двигает структуру, её двигают банки.
  B. ДИСПЛЕЙСМЕНТ (импульс): свеча с телом >= 1.6×ATR = агрессивный вход крупных.
     Розница так не двигает цену. Направление импульса = направление их позиции.
  C. ОРДЕР-БЛОКИ: последняя противоположная свеча ПЕРЕД импульсом = зона, где
     крупные набирали позицию. Возврат цены в зону = они часто защищают её
     повторными ордерами (вход с ними, не против них).
  D. FVG / ИМБАЛАНС: 3-свечный разрыв = крупные вошли так агрессивно, что не
     дали другой стороне исполниться. Незакрытый FVG = магнит и зона защиты.
  E. ПУЛЫ ЛИКВИДНОСТИ (равные хаи/лоу): крупным нужна чужая ликвидность чтобы
     ЗАКРЫТЬ позицию → цена ИДЁТ К ближайшему пулу. Это даёт ЦЕЛЬ, а не просто
     «снимут стопы»: пул = куда поведут цену.
  F. ПРЕМИУМ/ДИСКАУНТ (диапазон H4): крупные покупают дёшево (нижняя половина
     диапазона) и продают дорого. Покупка в премиуме = покупать у них с рук.
  G. СВИПЫ (съёмы хай/лоу дня/недели — из liquidity_traps): свежий съём =
     крупные только что собрали топливо → толкают в ОБРАТНУЮ сторону.

Каждый компонент голосует -2..+2. Сумма = sm_score (примерно -10..+10).
|score| >= 3.5 → крупные игроки ЗАДАЮТ направление прогноза (основа системы).
"""
import numpy as np
import pandas as pd

PIP = 0.0001


def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def find_swings(df, left=2, right=2):
    """Fractal swing highs/lows: list of (index, price)."""
    highs, lows = [], []
    h = df["high"].values
    l = df["low"].values
    for i in range(left, len(df) - right):
        win_h = h[i - left:i + right + 1]
        win_l = l[i - left:i + right + 1]
        if h[i] == win_h.max() and (win_h == h[i]).sum() == 1:
            highs.append((i, float(h[i])))
        if l[i] == win_l.min() and (win_l == l[i]).sum() == 1:
            lows.append((i, float(l[i])))
    return highs, lows


def market_structure(df, label):
    """HH/HL vs LH/LL + BOS/CHoCH on the last swings. Returns dict."""
    out = {"bias": 0, "state": "нейтральная", "event": None, "label": label}
    if df is None or len(df) < 40:
        return out
    highs, lows = find_swings(df)
    if len(highs) < 2 or len(lows) < 2:
        return out
    (i_h2, h2), (i_h1, h1) = highs[-2], highs[-1]   # h1 = latest swing high
    (i_l2, l2), (i_l1, l1) = lows[-2], lows[-1]
    close = float(df["close"].iloc[-1])
    bullish = h1 > h2 and l1 > l2     # HH + HL
    bearish = h1 < h2 and l1 < l2     # LH + LL
    if bullish:
        out["bias"], out["state"] = 2, "БЫЧЬЯ (HH+HL — серия растущих хаёв и лоу)"
        if close < l1:  # CHoCH: broke last higher-low
            out["bias"], out["state"] = -1, "СЛОМ бычьей структуры (CHoCH вниз)"
            out["event"] = f"цена закрылась ПОД последним растущим лоу {l1:.5f} — возможен разворот вниз"
        elif close > h1:
            out["event"] = f"BOS вверх: пробит свинг-хай {h1:.5f} — крупные продолжают покупать"
    elif bearish:
        out["bias"], out["state"] = -2, "МЕДВЕЖЬЯ (LH+LL — серия падающих хаёв и лоу)"
        if close > h1:
            out["bias"], out["state"] = 1, "СЛОМ медвежьей структуры (CHoCH вверх)"
            out["event"] = f"цена закрылась НАД последним падающим хаем {h1:.5f} — возможен разворот вверх"
        elif close < l1:
            out["event"] = f"BOS вниз: пробит свинг-лоу {l1:.5f} — крупные продолжают продавать"
    return out


def displacement(h1, lookback=24, k=1.6):
    """Latest impulsive candle (body >= k*ATR) in last `lookback` H1 bars."""
    out = {"dir": 0, "bars_ago": None, "pips": 0.0}
    if h1 is None or len(h1) < 40:
        return out
    a = _atr(h1)
    d = h1.tail(lookback).reset_index(drop=True)
    av = a.tail(lookback).reset_index(drop=True)
    best = None
    for i in range(len(d) - 1, -1, -1):
        body = abs(float(d["close"].iloc[i]) - float(d["open"].iloc[i]))
        if body >= k * float(av.iloc[i]):
            best = i
            break
    if best is None:
        return out
    row = d.iloc[best]
    out["dir"] = 1 if row["close"] > row["open"] else -1
    out["bars_ago"] = len(d) - 1 - best
    out["pips"] = abs(float(row["close"]) - float(row["open"])) / PIP
    return out


def order_block(h1, disp, tolerance_atr=0.25):
    """Order block = last opposite candle before the displacement candle.
    Active if price has retraced back into the zone (smart money defends it)."""
    out = {"active": False, "dir": 0, "zone": None, "txt": None}
    if not disp or disp["dir"] == 0 or disp["bars_ago"] is None:
        return out
    idx = len(h1) - 1 - disp["bars_ago"]           # displacement candle index
    want_bear = disp["dir"] == 1                   # bullish disp -> last bearish candle
    for j in range(idx - 1, max(idx - 8, 0), -1):
        row = h1.iloc[j]
        is_bear = row["close"] < row["open"]
        if is_bear == want_bear:
            lo, hi = float(row["low"]), float(row["high"])
            price = float(h1["close"].iloc[-1])
            tol = float(_atr(h1).iloc[-1]) * tolerance_atr
            if lo - tol <= price <= hi + tol:
                out.update(active=True, dir=disp["dir"], zone=(lo, hi))
                side = "бычий" if disp["dir"] == 1 else "медвежий"
                out["txt"] = (f"цена вернулась в {side} ордер-блок {lo:.5f}–{hi:.5f} "
                              f"(зона, где крупные набирали позицию перед импульсом) — "
                              f"они часто защищают её повторно")
            else:
                out.update(active=False, dir=disp["dir"], zone=(lo, hi))
            break
    return out


def fair_value_gaps(h1, lookback=48):
    """Unfilled 3-candle FVGs near price. Returns nearest above/below + bias."""
    out = {"bias": 0, "txt": None}
    if h1 is None or len(h1) < lookback + 3:
        return out
    d = h1.tail(lookback + 2).reset_index(drop=True)
    price = float(d["close"].iloc[-1])
    gaps = []
    for i in range(2, len(d)):
        # bullish FVG: high[i-2] < low[i]
        if float(d["high"].iloc[i - 2]) < float(d["low"].iloc[i]):
            lo, hi = float(d["high"].iloc[i - 2]), float(d["low"].iloc[i])
            if float(d["low"].iloc[i:].min()) > lo:      # not fully filled
                gaps.append({"dir": 1, "lo": lo, "hi": hi})
        # bearish FVG: low[i-2] > high[i]
        if float(d["low"].iloc[i - 2]) > float(d["high"].iloc[i]):
            lo, hi = float(d["high"].iloc[i]), float(d["low"].iloc[i - 2])
            if float(d["high"].iloc[i:].max()) < hi:
                gaps.append({"dir": -1, "lo": lo, "hi": hi})
    if not gaps:
        return out
    # price sitting inside a fresh FVG = smart money defending the imbalance
    for g in reversed(gaps):
        if g["lo"] <= price <= g["hi"]:
            out["bias"] = g["dir"]
            side = "бычьем" if g["dir"] == 1 else "медвежьем"
            out["txt"] = (f"цена стоит в {side} имбалансе (FVG {g['lo']:.5f}–{g['hi']:.5f}) — "
                          f"разрыв, оставленный агрессивным входом крупных; обычно защищается")
            return out
    return out


def liquidity_pools(h1, d1, now=None, tol_pips=3.0):
    """Equal highs/lows (двойные вершины/донышки) = пулы стопов. Крупным нужна
    эта ликвидность, чтобы ЗАКРЫТЬ позицию → цена тянется к ближайшему пулу.
    Возвращает draw-направление (куда поведут цену) и сам уровень-цель."""
    out = {"bias": 0, "target": None, "txt": None, "above": None, "below": None}
    if h1 is None or len(h1) < 60:
        return out
    highs, lows = find_swings(h1.tail(120).reset_index(drop=True))
    price = float(h1["close"].iloc[-1])
    tol = tol_pips * PIP
    eq_high, eq_low = None, None
    for a in range(len(highs)):
        for b in range(a + 1, len(highs)):
            if abs(highs[a][1] - highs[b][1]) <= tol and highs[b][1] > price:
                lvl = max(highs[a][1], highs[b][1])
                if eq_high is None or lvl < eq_high:
                    eq_high = lvl
    for a in range(len(lows)):
        for b in range(a + 1, len(lows)):
            if abs(lows[a][1] - lows[b][1]) <= tol and lows[b][1] < price:
                lvl = min(lows[a][1], lows[b][1])
                if eq_low is None or lvl > eq_low:
                    eq_low = lvl
    out["above"], out["below"] = eq_high, eq_low
    if eq_high is None and eq_low is None:
        return out
    d_up = (eq_high - price) / PIP if eq_high else 1e9
    d_dn = (price - eq_low) / PIP if eq_low else 1e9
    if d_up < d_dn:
        out["bias"], out["target"] = 1, eq_high
        out["txt"] = (f"ближайший пул ликвидности — РАВНЫЕ ХАИ {eq_high:.5f} "
                      f"(+{d_up:.0f} пипсов): там стопы продавцов, крупным выгодно "
                      f"дотянуть цену ТУДА, чтобы раздать позицию")
    else:
        out["bias"], out["target"] = -1, eq_low
        out["txt"] = (f"ближайший пул ликвидности — РАВНЫЕ ЛОУ {eq_low:.5f} "
                      f"(-{d_dn:.0f} пипсов): там стопы покупателей, крупным выгодно "
                      f"опустить цену ТУДА")
    return out


def premium_discount(h4, n=60):
    """Position in the H4 dealing range. Discount (<40%) favours BUY, premium (>60%) SELL."""
    out = {"bias": 0, "pos": 0.5, "txt": None}
    if h4 is None or len(h4) < 20:
        return out
    d = h4.tail(n)
    lo, hi = float(d["low"].min()), float(d["high"].max())
    price = float(h4["close"].iloc[-1])
    pos = (price - lo) / max(hi - lo, 1e-9)
    out["pos"] = pos
    if pos <= 0.40:
        out["bias"] = 1
        out["txt"] = (f"цена в ДИСКАУНТЕ ({pos*100:.0f}% диапазона H4) — зона, где "
                      f"крупные ПОКУПАЮТ (дёшево). Продавать здесь = продавать им в руки")
    elif pos >= 0.60:
        out["bias"] = -1
        out["txt"] = (f"цена в ПРЕМИУМЕ ({pos*100:.0f}% диапазона H4) — зона, где "
                      f"крупные ПРОДАЮТ (дорого). Покупать здесь = покупать у них с рук")
    else:
        out["txt"] = f"цена в середине диапазона H4 ({pos*100:.0f}%) — равновесие, эджа нет"
    return out


def analyze_smart_money(m15, h1, h4, d1, traps=None, now=None):
    """Главная функция: собирает все следы крупных игроков в один вердикт.

    Returns dict: score, bias ('BUY'/'SELL'/None), strong, lines (RU narrative),
    target (draw-on-liquidity level or None), components.
    """
    comp = {}
    score = 0.0
    lines = []

    st4 = market_structure(h4, "H4")
    st1 = market_structure(h1, "H1")
    comp["structure_h4"], comp["structure_h1"] = st4, st1
    score += st4["bias"] * 1.0          # H4 structure weight 2 (bias already ±2)
    score += st1["bias"] * 0.5
    lines.append(f"Структура H4: {st4['state']}" + (f" — {st4['event']}" if st4.get("event") else ""))
    lines.append(f"Структура H1: {st1['state']}" + (f" — {st1['event']}" if st1.get("event") else ""))

    disp = displacement(h1)
    comp["displacement"] = disp
    if disp["dir"]:
        score += disp["dir"] * 1.5
        side = "ВВЕРХ ⬆️" if disp["dir"] == 1 else "ВНИЗ ⬇️"
        lines.append(f"Импульс крупных: свеча-дисплейсмент {side} (~{disp['pips']:.0f} пипсов, "
                     f"{disp['bars_ago']}ч назад) — так двигают цену только большие деньги")
    else:
        lines.append("Импульса (дисплейсмента) за сутки нет — крупные не проявлялись агрессивно")

    ob = order_block(h1, disp)
    comp["order_block"] = ob
    if ob["active"]:
        score += ob["dir"] * 2.0
        lines.append(f"Ордер-блок: {ob['txt']}")

    fvg = fair_value_gaps(h1)
    comp["fvg"] = fvg
    if fvg["bias"]:
        score += fvg["bias"] * 1.0
        lines.append(f"Имбаланс: {fvg['txt']}")

    pools = liquidity_pools(h1, d1, now)
    comp["pools"] = pools
    if pools["bias"]:
        score += pools["bias"] * 1.0
        lines.append(f"Куда поведут цену: {pools['txt']}")

    pd_ = premium_discount(h4)
    comp["premium_discount"] = pd_
    if pd_["bias"]:
        score += pd_["bias"] * 1.0
    if pd_["txt"]:
        lines.append(f"Премиум/дискаунт: {pd_['txt']}")

    if traps and traps.get("sweeps"):
        s = traps["sweeps"][0]
        sweep_dir = 1 if s["bias"] == "BUY" else -1
        score += sweep_dir * (2.0 if s["w"] >= 2 else 1.0)
        lines.append(f"Свип: свежий съём {s['name']} ({s['level']:.5f}) — топливо собрано, "
                     f"крупные толкают {'ВВЕРХ' if sweep_dir == 1 else 'ВНИЗ'}")

    bias = "BUY" if score >= 2 else ("SELL" if score <= -2 else None)
    strong = abs(score) >= 3.5   # сильный вердикт → крупные задают направление
    verdict = ("КРУПНЫЕ ПОКУПАЮТ" if bias == "BUY" else
               "КРУПНЫЕ ПРОДАЮТ" if bias == "SELL" else
               "крупные не определились / накапливают")
    return {"score": round(score, 1), "bias": bias, "strong": strong,
            "verdict": verdict, "lines": lines, "target": pools.get("target"),
            "components": comp}


if __name__ == "__main__":
    # quick self-test on live data
    import requests

    def fetch(interval, rng, symbol="EURUSD=X"):
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                         params={"interval": interval, "range": rng},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        return pd.DataFrame({"time": pd.to_datetime(res["timestamp"], unit="s", utc=True),
                             "open": q["open"], "high": q["high"],
                             "low": q["low"], "close": q["close"]}).dropna().reset_index(drop=True)

    h1 = fetch("60m", "60d")
    m15 = fetch("15m", "5d")
    d1 = fetch("1d", "2y")
    d = h1.set_index("time")
    h4 = pd.DataFrame({"open": d["open"].resample("4h").first(),
                       "high": d["high"].resample("4h").max(),
                       "low": d["low"].resample("4h").min(),
                       "close": d["close"].resample("4h").last()}).dropna().reset_index()
    sm = analyze_smart_money(m15, h1, h4, d1)
    print("score:", sm["score"], "bias:", sm["bias"], "strong:", sm["strong"])
    print("verdict:", sm["verdict"])
    for ln in sm["lines"]:
        print(" -", ln)
