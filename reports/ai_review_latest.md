# 🤖 AI-обзор стратегии — авто-генерация

_Этот файл создаётся GitHub Actions workflow `ai_review.yml` каждый раз после завершения 5-часового цикла._

## 🧠 Эвристический AI-обзор (без LLM)

### Метрики
- **WR за последние 5 часов:** 33.3% (27 решений)
- **WR на 28-парном бэктесте:** —%
- **Деградировавшие пары:** EURCHF

### Диагноз
- ⚠ WR 33.3% сильно ниже break-even (55.6% для 80% binary).
- Пары в деградации (1): `EURCHF` — рассмотреть исключение из топа на 24ч.

### Рекомендации к параметрам
- Поднять `STRONG_CONFIDENCE` 88 → 90, `STRONG_RATIO` 0.55 → 0.60, `STRONG_PERSISTENCE` 80 → 100 (требовать все 5 баров за 5ч в одну сторону).
- В `app/cycle.py._select_strict` добавить временный blacklist для деградировавших пар: ['EURCHF'].

### Точки правки
- `app/cycle.py`: `STRONG_CONFIDENCE`, `STRONG_RATIO`, `STRONG_ADX_H1`, `STRONG_ADX_H4`, `STRONG_PERSISTENCE`, `PREMIUM_ADX_H1`, `PREMIUM_PERSISTENCE`, `MIN_PICKS`, `MAX_PICKS`
- `scripts/cycle_5h.py`: `MIN_TRADES_PER_DAY`, `TOP_N`
