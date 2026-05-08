# MACRO TABLE — 8 валют, актуальные ставки и тон ЦБ

> **Этот файл обновляется КАЖДЫЕ 5 ДНЕЙ.** Если дата `last_refreshed` старше 5 дней — данные могут быть устаревшими, обнови перед использованием в торговле.
>
> **last_refreshed:** 2026-05-08 (UTC)
> **next_refresh:** 2026-05-13 (UTC) или раньше при существенных изменениях

---

## Сводная таблица (8 мая 2026)

| ЦБ | Валюта | Ставка | Последнее изменение | Дата | Vote | Тон | След. заседание | Hawkish rank |
|---|---|---|---|---|---|---|---|---|
| RBA | **AUD** | **4.35%** | **Hike +25 bp** | **5 May 2026** | 8-1 | **Hawkish** (recent hike, sticky inflation, signals possible pause but tightening bias) | **17 Jun 2026** | **#1 (most hawkish)** |
| BoE | **GBP** | **3.75%** | Hold | 30 Apr 2026 | 8-1 (Pill voting hike) | **Hawkish** (MUFG sees 50bp tightening this year, possible from June) | **18 Jun 2026** | #2 |
| Fed | **USD** | **3.75%** | Cut −25 bp | 11 Dec 2025 | — | Neutral, hawkish drift on inflation. TD Securities: asymmetric downside risk into payrolls | TBD (next FOMC mid-Jun) | #3 |
| BoJ | **JPY** | **0.75%** | Hike +25 bp | 19 Dec 2025 | — | Hawkish but cautious; intervention threat at USD/JPY >155 | TBD (~late Jul) | #4 |
| BoC | **CAD** | **2.25%** | Hold | 29 Apr 2026 | — | Neutral; oil headwind | **4 Jun 2026** | #5 |
| RBNZ | **NZD** | **2.25%** | Hold | Feb 2026 | — | Neutral, no recent hike | **28 May 2026** | #6 |
| ECB | **EUR** | **2.00%** | Cut −25 bp | 5 Jun 2025 | — | Dovish, end of cycle | TBD | #7 |
| SNB | **CHF** | **0.00%** | Hold | Apr 2026 | — | Dovish, lowest rate in G10 | TBD | #8 (most dovish) |

---

## Ключевые drivers (8 мая 2026)

### Risk-on / Risk-off режим
- **Текущий:** **MIXED Risk-On** с подкоркой неопределённости от Iran
- **DXY:** 97.85 (падает с пика 100.48 March 31)
- **VIX:** 17.08 (низкий)
- **SPX:** 7,337 (рекордные high'и на этой неделе)
- **Gold:** $4,713 (растёт, +0.60%) — risk-off undertone
- **WTI Oil:** $96 (упал с пика $115 после Iran ceasefire hope)
- **Brent:** ~$110

### Геополитика
- **Iran ceasefire trade:** активная фаза с конца апреля. 4-week ceasefire. Угроза срыва — Trump talked of "very good talks", но Iran выставляет conditions (lifting US naval blockade)
- **Strait of Hormuz:** регулярные warnings IRGC, escort operation paused
- **US gasoline:** $4.54/gal — самая высокая с июля 2022

### Энергетический шок
- ECB and BoE cite "second-round effects" of energy on inflation
- Поддерживает hawkish bias для BoE
- Поддерживает dovish bias для ECB (slower transmission, weaker eurozone)
- AUD inflation от Middle East → RBA hike May 5

### Политика США
- Trump tariff threats (continued from 2025)
- Trade tariffs vs China/EU → CNH ↓, EUR ↓, USD ambiguous

---

## Ключевые события следующих 7 дней

| Дата (UTC) | Время | Валюта | Событие | Impact |
|---|---|---|---|---|
| Fri 8 May | 12:30 | USD | NFP, Unemployment Rate, Avg Earnings | **Tier 1** |
| Fri 8 May | 14:00 | USD | Fed Williams speech | Tier 2 |
| Mon 11 May | 06:00 | EUR | ECB ConfFinSpeak | Tier 2 |
| Tue 12 May | 12:30 | USD | CPI (April) | **Tier 1** |
| Tue 12 May | 06:00 | GBP | UK Employment | Tier 2 |
| Wed 13 May | 06:00 | GBP | UK GDP, Industrial Production | **Tier 1** |
| Wed 13 May | 12:30 | USD | Core PPI | Tier 2 |
| Thu 14 May | 12:30 | USD | Initial Jobless Claims, Retail Sales | Tier 2 |

> **Полный календарь:** `CALENDAR_2026.md`

---

## TOP-1 макро-bias на ближайшие 5 дней

### Прямые сетапы (BUY/SELL по макро)
1. **BUY AUD/CHF** — gap rank 7 (AUD #1 hawkish vs CHF #8 dovish), risk-on supports AUD, RBA hike fresh
2. **BUY GBP/CHF** — gap 6, BoE hawkish split bias, GBP supported by hike expectations
3. **BUY AUD/EUR** (= AUD/EUR or via EUR/AUD SELL) — gap 6, AUD hawkish vs EUR dovish
4. **BUY GBP/EUR** (or EUR/GBP SELL) — gap 5
5. **BUY AUD/CAD** — gap 4, AUD hawkish vs CAD oil-headwind
6. **BUY AUD/NZD** — gap 5, AUD recent hike vs NZD hold

### Условные сетапы (зависят от RoRo)
7. **BUY GBP/JPY** — gap 2 (only if Risk-On strong)
8. **SELL EUR/USD** — gap 4 (но zona противоречий: USD asymmetric downside per TD)

### Сетапы которых НЕ берём
- **USD-cross trades when DXY уходит вниз** — fundamental dis-alignment
- **JPY-pairs близко к BoJ intervention zones** — резкие движения
- **CAD trades без свежего oil-look** — oil flips daily

---

## Изменения с предыдущего refresh

(первый refresh — изменений нет)

**Ожидаемые изменения к следующему refresh (13 мая 2026):**
- US CPI 12 мая может изменить Fed expectation (+/− USD bias)
- UK GDP 13 мая может усилить/ослабить GBP

---

## Calibration log (для алгоритма CSM)

- **Weights в blended formula:** 50% D1 + 30% W1 + 20% M1
- **Macro tilt weight:** 0.20% per rank position
- **Изменений с v1.0:** нет

---

## Источники для refresh

1. https://www.bankofengland.co.uk/monetary-policy/interest-rates-and-bank-rate
2. https://www.federalreserve.gov/monetarypolicy/openmarket.htm
3. https://www.ecb.europa.eu/stats/policy_and_exchange_rates/key_ecb_interest_rates/html/index.en.html
4. https://www.boj.or.jp/en/mopo/mpmdeci/state_2026/index.htm
5. https://www.rba.gov.au/monetary-policy/int-rate-decisions/
6. https://www.rbnz.govt.nz/monetary-policy/official-cash-rate-decisions
7. https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/
8. https://www.snb.ch/en/iabout/monpol
9. https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm
10. ForexFactory: https://www.forexfactory.com/calendar
11. Saxo Bank: https://home.saxo (Ole Hansen weekly COT)
12. MUFG Research: https://www.mufgresearch.com/
13. ING THINK: https://think.ing.com/
14. TD Securities FX research

— конец MACRO_TABLE.md (refresh 2026-05-08) —
