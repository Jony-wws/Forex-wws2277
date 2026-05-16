"""Telegram progress bar — отслеживание прогресса долгих скриптов в Telegram.

Модуль создаёт одно сообщение в чате и обновляет его через ``editMessageText``
по мере выполнения. Поддерживает текстовый прогресс-бар (████░░░░░░), статус
шага, прошедшее и оставшееся время.

Run locally / в CI:

    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python scripts/cycle_5h.py

Если переменные окружения не заданы, модуль работает в no-op режиме —
печатает прогресс в stdout и НЕ падает. Это удобно для локальных тестов.

Пример:

    from telegram_progress import TelegramProgress

    progress = TelegramProgress(title="5-часовой цикл")
    progress.start("Подготовка данных...")
    progress.update(10, "Данные загружены")
    progress.update(30, "Анализ пар завершён")
    progress.update(60, "Бэктест завершён")
    progress.update(80, "Топ-3 выбран")
    progress.update(90, "Готовим отчёт...")
    progress.complete(full_report="<b>Итоговый отчёт...</b>")
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional


# Ширина прогресс-бара в символах (████░░░░░░ = 10 ячеек по умолчанию).
PROGRESS_BAR_WIDTH = 10

# Лимит на размер одного сообщения Telegram (4096 символов с запасом).
TELEGRAM_CHUNK_SIZE = 3900

# Таймаут HTTP-запросов к Telegram API.
HTTP_TIMEOUT_SEC = 15


def format_progress_bar(percent: float, width: int = PROGRESS_BAR_WIDTH) -> str:
    """Нарисовать текстовый прогресс-бар вида ``████░░░░░░``.

    ``percent`` обрезается в диапазоне ``[0, 100]``. ``width`` задаёт общую
    длину бара в символах.
    """
    pct = max(0.0, min(100.0, float(percent)))
    filled = int(round(pct / 100.0 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _format_duration(seconds: float) -> str:
    """Перевести секунды в читаемый русский формат: ``2 мин 15 сек``."""
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total} сек"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        if sec == 0:
            return f"{minutes} мин"
        return f"{minutes} мин {sec} сек"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours} ч"
    return f"{hours} ч {mins} мин"


def format_time_elapsed(seconds: float) -> str:
    """Сколько прошло времени с момента старта: ``2 мин 15 сек``."""
    return _format_duration(seconds)


def format_time_remaining(seconds_remaining: Optional[float]) -> str:
    """Сколько ещё осталось примерно: ``ориентировочно 3 мин``.

    Если оценка ещё не доступна (нулевой прогресс), возвращает
    ``ориентировочно — рассчитывается``.
    """
    if seconds_remaining is None or seconds_remaining <= 0:
        return "ориентировочно — рассчитывается"
    return f"ориентировочно {_format_duration(seconds_remaining)}"


def _calculate_eta(start_ts: float, percent: float,
                   now_ts: Optional[float] = None) -> Optional[float]:
    """Линейная экстраполяция оставшегося времени до 100%.

    Возвращает количество секунд до завершения или ``None``, если оценка
    невозможна (percent ≤ 0 или ≥ 100).
    """
    if percent is None:
        return None
    if percent <= 0 or percent >= 100:
        return None
    now = now_ts if now_ts is not None else time.time()
    elapsed = now - start_ts
    if elapsed <= 0:
        return None
    total_estimated = elapsed * 100.0 / float(percent)
    return max(0.0, total_estimated - elapsed)


# ── Низкоуровневые обёртки над Telegram API ──────────────────────────────


def send_progress_message(token: str, chat_id: str, text: str) -> Optional[int]:
    """Отправить начальное сообщение с прогрессом. Возвращает ``message_id``.

    При ошибке возвращает ``None`` и печатает диагностику в stdout.
    """
    if not token or not chat_id:
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=HTTP_TIMEOUT_SEC) as resp:
            body = resp.read().decode() or "{}"
        payload = json.loads(body)
        if not payload.get("ok", False):
            print(f"[telegram_progress] sendMessage not ok: {payload.get('description', '')}")
            return None
        result = payload.get("result") or {}
        msg_id = result.get("message_id")
        return int(msg_id) if msg_id is not None else None
    except Exception as e:  # noqa: BLE001
        print(f"[telegram_progress] sendMessage failed: {e}")
        return None


def update_progress_message(token: str, chat_id: str, message_id: int,
                            text: str) -> bool:
    """Обновить существующее сообщение через ``editMessageText``."""
    return _edit_message_text(token, chat_id, message_id, text)


def _edit_message_text(token: str, chat_id: str, message_id: Optional[int],
                       text: str) -> bool:
    """Низкоуровневый вызов Telegram ``editMessageText`` для обновления."""
    if not token or not chat_id or message_id is None:
        return False
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=HTTP_TIMEOUT_SEC) as resp:
            body = resp.read().decode() or "{}"
        payload = json.loads(body)
        if not payload.get("ok", False):
            desc = payload.get("description", "")
            # Telegram возвращает "message is not modified" если текст
            # совпадает с предыдущим — это не ошибка для нашего use-case.
            if "not modified" in desc:
                return True
            print(f"[telegram_progress] editMessageText not ok: {desc}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[telegram_progress] editMessageText failed: {e}")
        return False


def _send_chunked(token: str, chat_id: str, text: str,
                  chunk_size: int = TELEGRAM_CHUNK_SIZE) -> bool:
    """Отправить длинный текст как серию сообщений (Telegram лимит 4096)."""
    if not token or not chat_id or not text:
        return False
    chunks: list[str] = []
    remaining = text
    while remaining:
        chunks.append(remaining[:chunk_size])
        remaining = remaining[chunk_size:]
    ok_all = True
    for chunk in chunks:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=HTTP_TIMEOUT_SEC) as resp:
                resp.read()
        except Exception as e:  # noqa: BLE001
            print(f"[telegram_progress] send chunk failed: {e}")
            ok_all = False
    return ok_all


def complete_progress_message(token: str, chat_id: str,
                              message_id: Optional[int],
                              summary: str,
                              full_report: Optional[str] = None) -> bool:
    """Финальный шаг: обновить прогресс-сообщение на ``summary`` и при
    необходимости отправить ``full_report`` отдельными сообщениями.

    Длинный ``full_report`` нельзя засунуть в ``editMessageText`` из-за
    лимита 4096 символов — поэтому он уходит как новая серия сообщений.
    """
    ok = True
    if message_id is not None:
        ok = _edit_message_text(token, chat_id, message_id, summary) and ok
    if full_report:
        ok = _send_chunked(token, chat_id, full_report) and ok
    return ok


# ── Высокоуровневый класс ────────────────────────────────────────────────


class TelegramProgress:
    """Управление прогресс-сообщением в Telegram.

    Объект хранит ``message_id`` отправленного сообщения и таймер старта,
    рассчитывает ETA и рендерит прогресс-бар на каждом ``update()``.

    Если ``TELEGRAM_BOT_TOKEN``/``TELEGRAM_CHAT_ID`` не заданы — работает
    в no-op режиме (печатает прогресс в stdout), что удобно для локальных
    запусков и тестов.
    """

    def __init__(self,
                 title: str = "Прогресс выполнения",
                 token: Optional[str] = None,
                 chat_id: Optional[str] = None,
                 bar_width: int = PROGRESS_BAR_WIDTH) -> None:
        self.title = title
        env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        env_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.token = (token if token is not None else env_token).strip()
        self.chat_id = (chat_id if chat_id is not None else env_chat).strip()
        self.bar_width = bar_width
        self.message_id: Optional[int] = None
        self.start_ts: float = 0.0
        self.last_percent: float = 0.0
        self.last_status: str = ""
        self.last_render: str = ""
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            print("[telegram_progress] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
                  "не заданы — прогресс будет печататься локально")

    # ── рендеринг ────────────────────────────────────────────────────────

    def _render(self, percent: float, status: str) -> str:
        bar = format_progress_bar(percent, self.bar_width)
        elapsed = time.time() - self.start_ts if self.start_ts > 0 else 0.0
        eta = _calculate_eta(self.start_ts, percent) if self.start_ts > 0 else None
        lines: list[str] = [
            f"<b>⏳ {self.title}</b>",
            "",
            f"<code>{bar}</code>  <b>{percent:.0f}%</b>",
        ]
        if status:
            lines.append(f"<i>{status}</i>")
        lines.append("")
        lines.append(f"Прошло: <b>{format_time_elapsed(elapsed)}</b>")
        lines.append(f"Осталось: <b>{format_time_remaining(eta)}</b>")
        return "\n".join(lines)

    # ── публичный API ────────────────────────────────────────────────────

    def start(self, status: str = "Запуск...") -> None:
        """Отправить первоначальное сообщение и запустить таймер."""
        self.start_ts = time.time()
        self.last_percent = 0.0
        self.last_status = status
        text = self._render(0.0, status)
        self.last_render = text
        if not self.enabled:
            print(f"[telegram_progress] start: 0% — {status}")
            return
        self.message_id = send_progress_message(self.token, self.chat_id, text)
        if self.message_id is None:
            print("[telegram_progress] не удалось отправить начальное сообщение")

    def update(self, percent: float, status: str = "") -> bool:
        """Обновить прогресс. ``status`` опционален: если пуст, сохраняется
        предыдущий статус (полезно при инкрементальных тиках процента).
        """
        self.last_percent = float(percent)
        if status:
            self.last_status = status
        text = self._render(self.last_percent, self.last_status)
        if text == self.last_render:
            return True
        self.last_render = text
        if not self.enabled:
            print(f"[telegram_progress] {self.last_percent:.0f}% — {self.last_status}")
            return True
        if self.message_id is None:
            # Если start() не вызвали или предыдущая отправка упала —
            # пробуем отправить заново сейчас.
            self.message_id = send_progress_message(self.token, self.chat_id, text)
            return self.message_id is not None
        return update_progress_message(self.token, self.chat_id,
                                       self.message_id, text)

    def complete(self, full_report: Optional[str] = None,
                 summary: Optional[str] = None) -> bool:
        """Финальный шаг: пометить прогресс как 100%, опционально отправить
        полный отчёт отдельными сообщениями.

        Параметры
        ---------
        full_report
            Большой блок HTML-текста (итоговый отчёт цикла). Отправляется
            как НОВЫЕ сообщения, потому что ``editMessageText`` ограничен
            4096 символами.
        summary
            Короткий заменитель прогресс-бара. Если ``None`` — будет
            автоматический "✅ Завершено" с длительностью выполнения.
        """
        elapsed = time.time() - self.start_ts if self.start_ts > 0 else 0.0
        if summary is None:
            bar = format_progress_bar(100.0, self.bar_width)
            summary = (
                f"<b>✅ {self.title} — завершено</b>\n"
                f"<code>{bar}</code>  <b>100%</b>\n"
                f"Время выполнения: <b>{format_time_elapsed(elapsed)}</b>"
            )
        self.last_percent = 100.0
        self.last_render = summary
        if not self.enabled:
            print(f"[telegram_progress] complete: {summary}")
            if full_report:
                print("[telegram_progress] (full report пропущен — нет токена)")
            return True
        return complete_progress_message(
            self.token, self.chat_id, self.message_id,
            summary=summary, full_report=full_report,
        )
