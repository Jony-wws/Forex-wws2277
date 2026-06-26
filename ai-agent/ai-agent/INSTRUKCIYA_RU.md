# 🤖 Универсальный ИИ-Агент (Groq) — ПОЛНАЯ ИНСТРУКЦИЯ И РЕЗЕРВ

> **ВАЖНО для новой сессии:** скажи ИИ: «Прочитай этот репозиторий, папку ai-agent, файл ИНСТРУКЦИЯ.md и ai_agent.py — воссоздай мне точно такого же агента». ИИ всё восстановит с этого места.

---

## 📌 ЧТО ЭТО
Автономный ИИ-агент для Windows: управляет мышью/клавиатурой как человек, ВИДИТ экран, заходит на сайты, собирает данные, ЗАПОМИНАЕТ всё и делает ОТЧЁТЫ/ПРОГНОЗЫ. Основное применение — анализ Форекс + любые сложные задачи. Интерфейс русский, крупный шрифт, окно поверх всех.

---

## 🛠️ ПОЭТАПНО ЧТО БЫЛО СДЕЛАНО (последовательность установки)

**ПК:** Windows, экран 1920x1080. Python: `C:\Python311\python.exe`

### Шаг 1 — Python-библиотеки (PowerShell):
```powershell
& "C:\Python311\python.exe" -m pip install pyautogui requests pillow --upgrade
```
(pyautogui — мышь/клавиатура; requests — запросы к ИИ; pillow/PIL — скриншоты)
Проверка:
```powershell
& "C:\Python311\python.exe" -c "import pyautogui, requests; from PIL import ImageTk; print('OK')"
```

### Шаг 2 — Удалённый доступ с телефона (AnyDesk Portable):
```powershell
Invoke-WebRequest -Uri "https://download.anydesk.com/AnyDesk.exe" -OutFile "C:\Users\simular\Downloads\AnyDesk.exe"
Start-Process "C:\Users\simular\Downloads\AnyDesk.exe"
```
- **AnyDesk ID этого ПК: 1233841226**
- ⚠️ Chrome Remote Desktop и VNC НЕ работают — блокирует групповая политика Windows (MSI запрещены). Используем AnyDesk Portable (без установки).

### Шаг 3 — Выбор ИИ-движка:
- Пробовали Google Gemini — НЕ подошёл (бесплатная квота = 0). Старый Gemini-ключ: `AIzaSyAW6TUGua8E3LmfR0dE-Rz3w1u1KMnXa44` (не работает, квота).
- Пробовали Ollama локально — слишком медленно на CPU.
- **Выбран Groq** — бесплатный, очень быстрый (1-3 сек), видит экран. ✅

### Шаг 4 — Получение Groq-ключа:
Зайти на https://console.groq.com/keys → войти (Google/GitHub) → Create API Key.

### Шаг 5 — Сохранить код агента:
Файл `ai_agent.py` (в этом репо) → положить в `C:\Users\simular\ai_agent.py`.

### Шаг 6 — Запуск:
```powershell
Start-Process -FilePath "C:\Python311\python.exe" -ArgumentList "C:\Users\simular\ai_agent.py"
```

---

## 🔑 ВСЕ КЛЮЧИ И НАСТРОЙКИ
| Параметр | Значение |
|---|---|
| ИИ-движок | Groq (бесплатно) |
| Groq API ключ | (хранится локально в твоих файлах artifacts — НЕ в публичном GitHub ради безопасности; создай новый на console.groq.com) |
| Модель (зрение) | `meta-llama/llama-4-scout-17b-16e-instruct` |
| API URL | `https://api.groq.com/openai/v1/chat/completions` |
| Новый ключ | https://console.groq.com/keys → Create API Key → заменить GROQ_KEY |
| AnyDesk ID | **1233841226** |
| Google аккаунт | massaw750@gmail.com |
| Память | `C:\Users\simular\agent_memory.json` (создаётся сам) |
| Отчёты | `C:\Users\simular\agent_reports\` (создаётся сама) |

---

## ⚡ ОПТИМИЗАЦИЯ ПРОТИВ ВЫЛЕТОВ (v2 — ВАЖНО!)
Раньше агент ВЫЛЕТАЛ при задачах. Причины найдены и устранены в `ai_agent.py`:

1. **ГЛАВНАЯ причина — `ImageTk.PhotoImage` создавался в фоновом потоке.** Tkinter этого не допускает → краш. ✅ ФИКС: PhotoImage теперь создаётся ТОЛЬКО в главном потоке через `root.after()`.
2. **Глобальный перехват ошибок:** `root.report_callback_exception` — любая ошибка пишется в чат, окно НЕ закрывается.
3. **Все потоки в try/except** (`_safe_dispatch`, `_run` с finally) — поток никогда не уронит приложение.
4. **Все обновления окна** (msg, vision) через `root.after(0,...)` — нет фризов.
5. **Авто-установка библиотек** при старте (если чего-то нет).
6. **Ограничение памяти** (chat≤200, tasks≤100) — файл не разрастается.
7. **Скриншот сжат** (1280px, JPEG q50) — меньше ошибок сети, быстрее.
8. **Масштаб координат** для экранов >1280px.
9. **DPI awareness** — точные клики.

---

## 💬 КАК ПОЛЬЗОВАТЬСЯ
- **Вопрос** → агент отвечает в чате.
- **Задача** → агент уточняет «Понял так: ...» и спрашивает «Выполнить? напиши да» → пишешь **да** → выполняет сам пошагово (до 40 шагов), на каждом факте делает note, в конце — отчёт.
- **⛔ СТОП** — прервать. Панель **👁️** — что видит агент в реальном времени.
- Всё сохраняется в память и в папку отчётов автоматически.

---

## 🧩 АРХИТЕКТУРА КОДА
- `groq_chat()` — вызов Groq (4 повтора при 429).
- `classify()` — вопрос или задача.
- `agent_step()` — скриншот → ИИ возвращает JSON-действие.
- `do_action()` — click/type/press/scroll/open_app/goto_url/search/note/report/done.
- `load_memory()/save_memory()` — постоянная память JSON с лимитом.
- `AgentGUI` — интерфейс tkinter (все обновления через root.after).

---

## 🔒 БЕЗОПАСНОСТЬ
- Groq-ключ светился в чате — рекомендуется пересоздать на console.groq.com.
- Пароль пользователя светился ранее — рекомендуется сменить.

*Версия: v2 (оптимизирована против вылетов). Groq + зрение + память + отчёты.*
