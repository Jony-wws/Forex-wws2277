# windsurf-vnc-setup

Public web UI that lets the user drive **Windsurf** (running on the Devin
VM's display `:0`) from **Android Chrome** on a phone, with a separate
text-input field that bypasses Android's predictive on-screen keyboard
(which otherwise drops Cyrillic characters when typed straight into
plain noVNC).

> Этот каталог НЕ относится к форекс-системе из остального репозитория.
> Это автономный модуль для будущих сессий Devin: пользователь хочет
> открывать Windsurf со своего Android-телефона и общаться с Cascade
> (Claude) через одну ссылку.

---

## Что это и зачем

- Пользователь — на Android Chrome. Девин VM — Linux + TigerVNC на `:0`.
- В обычном noVNC ввод кириллицы и предиктивная клавиатура Android
  пропускают буквы (например, «ч», «щ», «ь»). Это известный баг noVNC.
- Решение: **одна страница** на одном порту 5050 содержит:
  - **VNC-вьюпорт** (через библиотеку `noVNC core/rfb.js`, подключение
    к локальному TigerVNC `127.0.0.1:5901` через WebSocket-прокси,
    встроенный в этот же aiohttp-сервер).
  - **Поле ввода** — обычный `<textarea>` HTML. Кнопка «Отправить»
    делает `fetch /type`, сервер активирует окно Windsurf и
    проигрывает текст через `xdotool type --` в активный input
    Cascade. Cyrillic проходит без потерь.
  - **Кнопки спецклавиш**: Enter, Tab, Esc, Bksp, стрелки, Ctrl+L/A/C/V.
  - **«Hide panel»** — спрятать поле ввода и оставить только VNC.

Всё за одной basic-auth Devin-туннеля → пользователю достаточно одной
ссылки `https://user:<password>@<host>/`.

---

## Файлы

```
windsurf-vnc-setup/
├── README.md         (этот файл)
├── setup.sh          one-shot установщик (запускать в Devin VM)
├── combined.py       aiohttp-сервер (порт 5050)
└── combined.html     UI (VNC + textarea + key buttons)
```

---

## Быстрый запуск (для будущего Devin)

Запустить **внутри** активной сессии Devin:

```bash
bash windsurf-vnc-setup/setup.sh
```

Затем вызвать инструмент `deploy` с командой `expose` и `port=5050`.
Девин выдаст публичный URL вида:
```
https://user:<token>@<id>-tunnel-<rand>.devinapps.com
```
Эту ссылку отправить пользователю в Android Chrome — он сразу попадёт
на страницу с VNC + полем ввода.

После открытия страницы пользователь:
1. Видит экран Windsurf сверху, поле ввода снизу.
2. Внизу должно показаться `Connected` (статус VNC).
3. Cascade-чат уже открыт, модель Claude выбрана (см. ниже про
   модель — если не выбрана, помоги пользователю).
4. Пишет в поле, жмёт **«Отправить (Enter)»** → текст идёт прямо в
   Cascade, не пропуская букв.

---

## Архитектура

```
Android Chrome ──HTTPS──► devinapps.com tunnel (basic auth)
                              │
                              ▼
                      VM port 5050 (aiohttp combined.py)
                       │      │           │
              static  │  /websockify     /type, /key
              HTML/JS │  WebSocket       POST
                      │      │           │
                      ▼      ▼           ▼
              combined.html  ┌───────┐  xdotool
              + RFB lib  ───►│ TCP   │  ───► windsurf window (:0)
              (browser)      │ 5901  │       Cascade chat input
                             └───────┘
                            TigerVNC
```

Ключевые приёмы:

1. **WebSocket→TCP прокси** написан на чистом aiohttp, около 30 строк
   (см. `websockify_handler` в `combined.py`).
2. **noVNC только как библиотека** (`core/rfb.js`) — НЕ их `vnc.html`.
   Их UI 1.0/1.5 ломается на Android Chrome
   (`Cannot read properties of null at addTouchSpecificHandlers`).
   Своя HTML страница использует `new RFB(div, 'wss://.../websockify',
   {credentials:{password:'devin'}}); rfb.scaleViewport = true;`.
3. **Фокус Cascade input**: НЕ через `Ctrl+L` (он то открывает, то
   закрывает панель — ненадёжно). Вместо этого `xdotool mousemove
   <x> <y> click 1` где `x = 75% ширины окна, y = 92% высоты` —
   там всегда находится поле ввода Cascade.
4. **Кириллица через xdotool**: обязательно нужен `LC_ALL=C.utf8` в
   окружении подпроцесса, иначе xdotool валится с
   `Invalid multi-byte sequence`. Также добавляем русскую раскладку
   (`setxkbmap -layout us,ru`).
5. **CORS**: middleware добавляет `Access-Control-Allow-*` на все
   ответы — чтобы fetch с любого Origin (включая открытие через
   `https://user:pass@`) работал.
6. **Fetch URL без credentials**: страница строит абсолютный URL
   как `window.location.protocol + '//' + window.location.host`
   (в `host` нет `user:pass`). Иначе Chrome выдаёт
   `Request cannot be constructed from a URL that includes credentials`.

---

## Технические детали и значения

| Что | Значение |
|---|---|
| Версия Windsurf | 2.2.17-1778044319 |
| Бинарь | `/usr/bin/windsurf`, ресурсы `/usr/share/windsurf/` |
| TigerVNC | уже работает в Devin VM, `localhost:5901`, display `:0` |
| Пароль VNC | `devin` (зашит в `PASSWORD` в combined.html) |
| Версия noVNC | 1.5.0 (распакована в `/home/ubuntu/novnc-master/`) |
| aiohttp-сервер | `/home/ubuntu/typebridge/combined.py`, порт 5050 |
| Разрешение экрана | 600x1067 (портрет, удобно для Android) |
| Раскладка | `us,ru` с переключением Alt+Shift |
| xdotool путь | `/opt/.devin/package/custom_binaries/xdotool` |
| Cascade focus coords | x = 0.75 × W, y = 0.92 × H окна Windsurf |
| Команда меню «фокус Cascade» | `Cascade: Focus on Cascade View` |

---

## Возможные грабли

1. **403 при клонировании репо**: использовать
   `https://github.com/...` без токена (Devin сам подставит auth);
   если 403 на одном из репо пользователя — попробовать другой
   (Forex-wws2277 — действующий, остальные могли быть архивированы).
2. **`xdotool type` валится с `Invalid multi-byte sequence`**: добавить
   `LC_ALL=C.utf8` в env подпроцесса.
3. **Cascade закрывается после Ctrl+L**: НЕ использовать `Ctrl+L`
   как способ фокуса — он toggles. Кликать мышкой по координатам.
4. **Текст уходит «в никуда»**: значит активным окном стал не
   Windsurf, а Chrome (на VM). Перед typing всегда делать
   `xdotool windowactivate --sync <wid>` где wid берётся динамически
   через `wmctrl -l` по точному имени `"Windsurf"`.
5. **noVNC 1.0 на Android выдаёт `Cannot read properties of null`**:
   не использовать `vnc.html` — только `combined.html` со своим
   минимальным UI.
6. **VNC-вьюпорт прыгает в размере**: использовать в URL
   `?resize=scale` (а не `remote`); в combined.html уже стоит
   `rfb.scaleViewport = true; rfb.resizeSession = false;`.
7. **deploy expose URL «не пускает» с user:pass в адресе**: Chrome
   на Android проглатывает basic-auth из URL обычно, но если нет —
   пользователь должен ввести логин/пароль в pop-up. **Никогда не
   класть user:pass в `fetch()`** — отсюда баг
   `Request cannot be constructed from a URL that includes
   credentials`.

---

## Что сделать в новой сессии Devin (чек-лист)

1. Прочитать этот README и `setup.sh`.
2. Проверить что TigerVNC уже работает: `ss -tlnp | grep 5901`.
3. Запустить `bash windsurf-vnc-setup/setup.sh`.
4. `deploy expose port=5050` → получить публичный URL.
5. Отправить пользователю одну ссылку.
6. Открыть в Cascade нужную модель Claude (последнюю из доступных
   в Windsurf на момент сессии — модели меняются: на момент сборки
   была видна `Claude Opus 4.7 Medium`).
7. По желанию пользователя — почистить переписку Cascade.

Готово.
