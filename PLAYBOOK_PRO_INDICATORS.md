# PRO INDICATORS — Institutional Toolset (Chapter 33)

> **Назначение:** Дополнение к `PLAYBOOK_FOREX28_2026.md`. Топ-уровень индикаторов которые реально используют institutional банки, prop-дески и профессиональные трейдеры. Это НЕ замена базовых (RSI/MACD/Stoch/EMA), а **дополнительный confluence-слой**.
>
> **Принцип:** Каждый из этих индикаторов смотрим в TradingView (бесплатно, без логина). Применяем поверх 8-фильтр чек-листа из Главы 26. PRO-индикатор НЕ даёт сам по себе сигнал — он либо подтверждает, либо опровергает уже существующий confluence.
>
> **Версия:** v1.0 (2026-05-08), добавлено по запросу Jony.

---

## 33.1. Volume Profile / VPVR (Visible Range Volume Profile)

### Что это
Гистограмма объёма по горизонтали — показывает на каких ценах накоплен максимальный traded volume.

### Ключевые точки
- **POC (Point of Control)** — цена с максимальным объёмом за период. Это **«магнит»**: рынок возвращается к POC чаще чем к любой другой цене.
- **VAH (Value Area High)** — верхняя граница 70% объёма.
- **VAL (Value Area Low)** — нижняя граница 70% объёма.
- **HVN (High Volume Node)** — кластеры объёма (поддержка/сопротивление).
- **LVN (Low Volume Node)** — пустые зоны (цена их быстро проходит).

### Как использовать в нашей системе
- **Confluence #9 (бонус):** если entry уровень совпадает с POC/HVN — **+5%** к probability.
- **Антиловушка:** если цена движется к LVN — высокая вероятность fast move (можем не успеть). Если к HVN — высокая вероятность отбоя.
- **Target:** для 5h binary смотрим где будет POC через 5 часов = ожидаемый close.

### TradingView setup
- Indicator → "Volume Profile Visible Range" (бесплатный)
- Period: D1 для краткосрочных, W1 для swing.

### Forex caveat
В spot Forex объём = **tick volume** (количество тиков), а не реальный contract volume. Это approximation, но всё равно работает на ликвидных парах (USD/EUR/GBP/JPY majors).

---

## 33.2. Anchored VWAP (Volume Weighted Average Price)

### Что это
Средневзвешенная по объёму цена, привязанная к конкретному событию (session open, news event, swing high/low).

### Формула
```
VWAP_t = Σ (price_i × volume_i) / Σ volume_i  (от anchor до t)
```

### Anchor points (где якорить)
1. **Daily VWAP:** anchor = 00:00 UTC текущего дня (или 21:00 UTC прошлого = NY close).
2. **Session VWAP:** anchor = London open (07:00 UTC) или NY open (13:00 UTC).
3. **News VWAP:** anchor = bar того момента когда вышла NFP/CPI/FOMC.
4. **Swing VWAP:** anchor = последний значимый swing high (для SELL bias) или swing low (для BUY).
5. **Weekly VWAP:** anchor = Sunday/Monday market open.

### Как использовать
- Цена **выше VWAP** = bulls in control с момента anchor; **ниже** = bears.
- Откат к VWAP = **institutional fair value pullback** — лучшая зона для entry в направлении тренда.
- VWAP с σ-bands (±1σ, ±2σ) — extreme deviation zones (mean-reversion candidates).

### Confluence в нашей системе
- **+5% probability** если entry на retracement к Daily VWAP в направлении CSM.
- **−10%** если вход против Daily VWAP без оправдания.

### TradingView
- Indicator → "Anchored VWAP" (бесплатный, нужно вручную поставить anchor).

---

## 33.3. Cumulative Volume Delta (CVD)

### Что это
Running sum (buy_volume − sell_volume). Показывает **давление покупателей vs продавцов** во времени.

### Approximation для Forex (spot не даёт bid/ask volume для retail)
```
delta_bar = sign(close - open) × volume
CVD_t = Σ delta_bar from anchor to t
```
Если close > open → delta positive (buy pressure), если close < open → delta negative.

### Как использовать
- **CVD divergence:** цена делает new high, но CVD делает lower high → buy pressure исчерпан → разворот вниз. (То же самое для лоу.)
- **CVD trend:** если CVD растёт + цена растёт → trend healthy. Если CVD flat + цена растёт → ослабление.
- **CVD spike + price doesn't follow** = absorption (institutional поглощение) → разворот.

### Confluence
- **+5%** если CVD aligned с направлением сигнала.
- **−10%** если CVD divergence на entry timeframe (H1/H4).

### TradingView
- Indicator → "Cumulative Volume Delta" (несколько бесплатных версий, например LonesomeTheBlue или TradingLatino).

---

## 33.4. Ichimoku Cloud (Ichimoku Kinko Hyo)

### Что это
Японская комплексная trend система. **Топ-индикатор для JPY-пар** (используется BoJ-aligned банками).

### 5 компонентов
1. **Tenkan-sen (Conversion Line)** = (9-period high + low) / 2 — short-term momentum.
2. **Kijun-sen (Base Line)** = (26-period high + low) / 2 — medium-term momentum.
3. **Senkou Span A** = (Tenkan + Kijun) / 2, shifted 26 forward — leading edge cloud.
4. **Senkou Span B** = (52-period high + low) / 2, shifted 26 forward — slower edge cloud.
5. **Chikou Span (Lagging)** = current close, shifted 26 backward — confirmation.

### Облако (Kumo)
Зона между Senkou A и Senkou B. **Зелёное облако** (A>B) = bullish bias, **красное** (B>A) = bearish.

### Сигналы
- **Price above cloud + cloud green + Tenkan>Kijun + Chikou above price 26 bars ago** = **strong BUY** (confluence 5/5).
- **Price inside cloud** = неопределённость, не торгуем.
- **TK Cross** (Tenkan crossing Kijun) — momentum signal.
- **Cloud twist** (Senkou A пересекает Senkou B в будущем) — major trend change.

### Confluence в нашей системе
- **+10%** если ВСЕ 5 компонентов aligned (full Ichimoku confluence).
- **+5%** если 3-4 компонента aligned.
- **−10%** если price inside cloud (неопределённость).
- Особенно сильно для JPY-пар, GBP/JPY, EUR/JPY, USD/JPY.

### TradingView
- Indicator → "Ichimoku Cloud" (встроенный, дефолтные параметры 9/26/52).

---

## 33.5. Pivot Points (Standard, Fibonacci, Camarilla)

### Что это
Расчётные уровни поддержки/сопротивления на основе предыдущего бара (D, W, M).

### Standard Pivot
```
PP = (H + L + C) / 3
R1 = 2*PP - L
S1 = 2*PP - H
R2 = PP + (H - L)
S2 = PP - (H - L)
R3 = H + 2*(PP - L)
S3 = L - 2*(H - PP)
```

### Fibonacci Pivot
Заменяет 2× коэффициенты на 0.382 / 0.618 / 1.000:
```
R1 = PP + 0.382 * (H - L)
R2 = PP + 0.618 * (H - L)
R3 = PP + 1.000 * (H - L)
S1 = PP - 0.382 * (H - L)
... аналогично
```

### Camarilla Pivot (используется prop-десками)
```
H4 = ((H - L) * 1.1 / 2) + C
H3 = ((H - L) * 1.1 / 4) + C
L3 = C - ((H - L) * 1.1 / 4)
L4 = C - ((H - L) * 1.1 / 2)
```
**H3/L3** — основные intraday pivots, **H4/L4** — breakout levels.

### Как использовать
- **PP** — нейтральная зона (gravity). Цена выше = bull bias, ниже = bear.
- **R1/S1** — первая реакция. Большинство дней цена ходит в `S1...R1` коридоре.
- **R2/S2** — extreme zones (mean-reversion candidates).
- **R3/S3** — реально extreme, рідко достигаются.

### Confluence
- **+5%** если entry pulled back to PP/S1/R1 в направлении CSM.
- **+5%** если target = R2/S2 (5h binary close = ~70% случаев в зоне S1-R1, реально ходит до R2/S2).

### TradingView
- Indicator → "Pivot Points Standard" / "Fibonacci" / "Camarilla" (все бесплатные).

---

## 33.6. Bollinger Band Squeeze + Width

### Что это
Bollinger Bands = MA ± 2σ. **Squeeze** = период когда σ резко сужается (низкая волатильность) → впереди **expansion** (мощное движение).

### Bollinger Band Width (BBW)
```
BBW = (Upper - Lower) / Middle
```
Когда BBW падает в 5-й percentile последних 100 баров → **squeeze active**.

### Сигнал expansion
1. BBW < 5th percentile — squeeze в процессе.
2. Цена выходит из bands в одну из сторон — **breakout направление подтверждено**.
3. Volume spike на breakout bar — institutional подтверждает.

### Confluence в нашей системе
- **+15%** если BB squeeze active + breakout in direction of CSM/D1 trend (это самый мощный сетап для 5h binary).
- **−5%** если BBW > 95th percentile (рынок overextended, разворот ближе чем продолжение).

### TradingView
- Indicator → "Bollinger Bands" + "Bollinger Bands Width" (оба встроенные).

---

## 33.7. VSA (Volume Spread Analysis, Tom Williams)

### Что это
Метод анализа отдельных баров: range (spread) × volume × close position. Идентифицирует **аномалии** где institutional игроки видны.

### Главные паттерны VSA

#### 1. **Stopping Volume**
- Очень большой volume bar после downtrend
- Wide range, close in upper half
- Значит: smart money покупает, downtrend заканчивается.
- **Сигнал:** разворот вверх близко.

#### 2. **No Demand**
- Маленький range bar в uptrend
- Низкий volume
- Значит: покупателей нет, тренд устал.
- **Сигнал:** возможный pullback.

#### 3. **No Supply**
- Маленький range bar в downtrend
- Низкий volume
- Значит: продавцов нет, downtrend остановлен.
- **Сигнал:** разворот вверх.

#### 4. **Climactic Action (Buying/Selling Climax)**
- Очень wide range bar в направлении тренда
- Огромный volume
- Close против тренда (если selling climax — close в верхней половине)
- **Сигнал:** разворот в течение 1-3 баров.

#### 5. **Upthrust / Spring** (Wyckoff)
- Spring: пробой вниз last swing low с reversal close
- Upthrust: пробой вверх last swing high с reversal close
- **Сигнал:** liquidity sweep + reversal.

### Confluence
- **+10%** если VSA pattern aligned с CSM/D1 trend.
- **−15%** если VSA показывает climactic exhaustion в направлении нашего сигнала.

### TradingView
- Нет single VSA indicator. Использовать:
  - "Volume" (встроенный)
  - + bar-by-bar inspection
  - + "Anomaly Volume Bars" (LonesomeTheBlue VSA — бесплатно)

---

## 33.8. Hurst Exponent (трендовость рынка)

### Что это
Статистический коэффициент персистентности (0 to 1).
- **H > 0.5** — тренд (movement persistent)
- **H < 0.5** — mean-reverting (range-bound)
- **H = 0.5** — random walk

### Как использовать
- **H > 0.6** на H4 → строго trend-following сетапы (наша 8-фильтр система работает идеально).
- **H < 0.4** на H4 → range-bound, 8-фильтр trend система **не работает**, лучше пропуск.
- **H ≈ 0.5** → ambiguity, низкое качество сигналов.

### Confluence
- **+5%** если H > 0.55 на H4 для нашей пары.
- **−10%** если H < 0.45 (ranging market — наша trend-методика не подходит).

### TradingView
- Indicator → "Hurst Exponent" (поиск, есть несколько бесплатных, например ozakar или Hurst-Cycle).

---

## 33.9. ATR Trailing Stop / Chandelier Exit

### Что это
Volatility-adaptive stop level. **Chandelier Exit** — trailing stop на основе high/low ± multiplier × ATR.

### Формула (Chandelier для long)
```
chandelier_long = highest(high, 22) - 3 × ATR(22)
```
Если цена закроется ниже этого уровня — exit. То же зеркально для short.

### Применение в Binary Options
У нас НЕТ stop-loss (преимущество binary). Но Chandelier Exit полезен как **«психологический ориентир»**:
- Если цена пересекла `chandelier_long` после entry → trade «висит на волоске». Не паникуем (5h всё равно), но **в журнал записываем**.
- Если по closing time цена осталась выше `chandelier_long` после reset → структура держится → можем держать сценарий следующие 5h.

### Confluence
- **+3%** если entry выше последнего Chandelier reset (тренд healthy).

### TradingView
- Indicator → "Chandelier Exit" (встроенный или ChartArt версия).

---

## 33.10. Liquidity Zones (Equal Highs/Lows + Round Numbers)

### Что это
Места где сидят stop-losses retail трейдеров, и куда smart money ходит за ликвидностью (см. Глава 15 PLAYBOOK).

### Как идентифицировать
1. **Equal Highs (EQH):** два или более pivot highs на одном уровне ±5 пипсов.
2. **Equal Lows (EQL):** аналогично снизу.
3. **Round Numbers:** xx.000 / xx.500 (для USDJPY 152.00, 152.50; для EURUSD 1.1000, 1.1050).
4. **Session Highs/Lows:** Asian range high/low (часто sweep на London open).
5. **Previous Daily High/Low (PDH/PDL):** магнит в течение дня.
6. **Previous Weekly High/Low (PWH/PWL):** магнит в течение недели.

### Как использовать
- Если cuy entry **сразу выше EQH** → это hunting territory, smart money пойдёт за ликвидностью → возможен fakeout.
- **Лучший entry:** ПОСЛЕ liquidity sweep, когда видно что ликвидность снята.

### Confluence
- **+5%** если entry после liquidity sweep в направлении CSM (institutional accumulation/distribution complete).
- **−10%** если entry непосредственно НА EQH/EQL без sweep (вероятный stop hunt).

### TradingView
- Indicator → "Liquidity Levels" (LuxAlgo бесплатный, или ChartPrime).
- Для round numbers: рисуем вручную.

---

## 33.11. Practical Combo: top-tier 5h Binary setup

### Идеальный 5h-сетап (использует ВСЕ PRO-индикаторы)

```
1. CSM gap >= 4 (Глава 26 #1)
2. D1 + H4 + H1 trend aligned (Глава 26 #2-3)
3. ADX H4 > 25 (Глава 26 #4)
4. EMA 8 > 21 > 55 H4 (Глава 26 #5)
5. London / Overlap session (Глава 26 #6)
6. No news 5h window (Глава 26 #7)
7. Fundamental support (Глава 26 #8)

PRO LAYER:
8. Price ABOVE Daily VWAP (BUY) или BELOW (SELL)
9. CVD aligned с direction (CVD растёт = BUY confluent)
10. Ichimoku 5/5 components aligned (для JPY-пар)
11. BB Squeeze active или just expanded в нашу сторону
12. VSA: нет climactic exhaustion в нашу сторону
13. Hurst > 0.55 на H4
14. Liquidity sweep уже произошёл (entry после sweep)
15. Volume Profile: entry near HVN/POC, target near opposite VAH/VAL
```

**Если 14-15/15 → probability = 88-92%** (cap).

### Probability formula с PRO-слоем
```
base_p = 50 + (filters_passed - 5) * 4   # для 8-фильтр базовой
pro_bonus = sum of PRO confluence bonuses (5, 5, 10, 15, 10, 5, 5 = max +55%)
ADR_penalty = if adr_used > 60% then -10 else 0
news_penalty = if news_within_5h then -100 (skip)

final_p = min(92, base_p + pro_bonus + ADR_penalty)
```

### Минимальная PRO confluence для входа
- Базовая 7-8/8 + минимум **3 PRO confluence** (например VWAP + CVD + Ichimoku).
- Без PRO confluence → max probability cap **78%** (надёжно но не топ).

---

## 33.12. Anti-Indicator-Stacking Rule

**Predator trap:** добавление кучи индикаторов = иллюзия точности. На самом деле большинство индикаторов **коррелированы** (RSI/MACD/Stochastic — все momentum, повторяют друг друга).

### Правило
- Одна категория = один индикатор:
  - Momentum: RSI **или** Stoch (не оба)
  - Trend: EMA-stack **или** Ichimoku (не оба отдельно — Ichimoku включает trend logic)
  - Volume: Volume Profile **или** VSA **или** CVD (выбираем один-два)
  - Volatility: ATR **или** BB (выбираем по контексту)
- **Максимум 7 индикаторов на графике одновременно.** Иначе шум.

### Recommended PRO stack (на TradingView для 5h binary)
1. **EMA 8/21/55/200** (trend ribbon)
2. **ADX(14)** (trend strength)
3. **Ichimoku Cloud** (для JPY-пар, optional)
4. **Volume Profile VPVR (D1 anchor)** (institutional levels)
5. **Anchored VWAP (Daily anchor)** (fair value)
6. **Bollinger Bands (20, 2)** + BBW (squeeze detector)
7. **Liquidity Levels (LuxAlgo)** (EQH/EQL/sweeps)

Этого достаточно. RSI/MACD/CCI оставляем для базового анализа из основной книги.

---

## 33.13. Правило применения PRO-индикаторов

### Sequence (строгая последовательность)
1. Сначала проходим **8-фильтр базового чек-листа** (Глава 26).
2. Если 7-8/8 пройдено → **смотрим PRO confluence** (это Глава 33).
3. Если PRO confluence ≥3 из 8 → даём вход.
4. Если PRO confluence <3 → **снижаем probability на 5-10%** или пропускаем.

### Никогда
- Никогда **не используем PRO-индикатор как самостоятельный сигнал**. Только confluence layer.
- Никогда **не считаем PRO-индикаторы выше iron rules**. Если Iron Rule #1 (нет 70%+) — пропуск, даже если все PRO-индикаторы кричат «вход».

---

## 33.14. Источники и обучение

### Книги (must-read для PRO-уровня)
1. **Tom Williams** — "Master the Markets" (VSA bible)
2. **John Murphy** — "Technical Analysis of the Financial Markets" (foundation)
3. **Anna Coulling** — "A Complete Guide to Volume Price Analysis"
4. **James Dalton** — "Mind Over Markets" (Volume Profile / Market Profile)
5. **Steidlmayer & Hawkins** — "Steidlmayer on Markets" (TPO origin)

### Web/Video
- **TradingLatino** (YouTube) — Volume Profile + Order Flow on forex
- **Inner Circle Trader (ICT)** — институциональный SMC framework
- **Wyckoff Analytics** — фазы рынка + Spring/Upthrust

### Forex specifics
- **COT report** (CFTC) — еженедельный institutional positioning, https://www.cftc.gov/MarketReports/CommitmentsofTraders/
- **OANDA Order Book** — retail positioning (contrarian indicator), https://www.oanda.com/forex-trading/analysis/orderbook
- **MyFXBook Sentiment** — community positioning, https://www.myfxbook.com/community/outlook

---

## 33.15. Roadmap (v1.1+)

- [ ] **Order Flow (Footprint) integration** — нужны платные feeds (CME, ICE) или Bookmap. Для retail Forex есть suboptimal proxies.
- [ ] **Auto-detect Wyckoff phases** через Python (Spring/Upthrust patterns).
- [ ] **Real-time COT report** ingestion для weekly bias updates.
- [ ] **Custom Hurst exponent calculator** в `forex_analysis/`.
- [ ] **Anchored VWAP auto-anchor** на news events (NFP/CPI/FOMC).

---

— конец PLAYBOOK_PRO_INDICATORS.md (Chapter 33) v1.0 —
