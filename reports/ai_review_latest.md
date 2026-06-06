# 🤖 AI-обзор стратегии — авто-генерация

_Этот файл создаётся GitHub Actions workflow `ai_review.yml` каждый раз после завершения 5-часового цикла._

## 🧠 Эвристический AI-обзор (без LLM)

### Метрики
- **WR за последние 5 часов:** —% (0 решений)
- **WR на 28-парном бэктесте:** —%
- **Деградировавшие пары:** EURNZD

### Диагноз
- WR в норме — текущая жёсткость работает.
- Пары в деградации (1): `EURNZD` — рассмотреть исключение из топа на 24ч.

### Рекомендации к параметрам
- В `app/cycle.py._select_strict` добавить временный blacklist для деградировавших пар: ['EURNZD'].

### Точки правки
- `app/cycle.py`: `STRONG_CONFIDENCE`, `STRONG_RATIO`, `STRONG_ADX_H1`, `STRONG_ADX_H4`, `STRONG_PERSISTENCE`, `PREMIUM_ADX_H1`, `PREMIUM_PERSISTENCE`, `MIN_PICKS`, `MAX_PICKS`
- `scripts/cycle_5h.py`: `MIN_TRADES_PER_DAY`, `TOP_N`
