"""JSON-логи бота в stdout (006 §7.5): без текстов сообщений и аудио, токен бота вырезается."""

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

NO_TELEGRAM = (
    "Нет связи с api.telegram.org — проверьте интернет или VPN. "
    "Бот повторит сам, перезапускать не нужно."
)
TOKEN_CONFLICT = (
    "Этот токен уже использует другой запущенный бот (вторая копия Sobesnik или другой скрипт). "
    "Остановите её: docker compose stop bot в той папке. Пока она работает, сообщения уходят ей."
)
HUMAN_ERRORS = {
    "TelegramConflictError": TOKEN_CONFLICT,
    "TelegramNetworkError": NO_TELEGRAM,
    "TelegramServerError": NO_TELEGRAM,
}
"""Ошибки опроса Telegram, которые aiogram пишет строкой «Failed to fetch updates - <класс>: …»."""
HUMAN_ERRORS_EVERY_S = 60.0

_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


class RedactingFormatter(logging.Formatter):
    """Оборачивает форматтер и заменяет секреты на `***` в готовой строке — где бы они ни были:
    в сообщении, в `extra`, в трейсбеке (токен входит в URL запросов к Telegram)."""

    def __init__(self, inner: logging.Formatter, secrets: list[str]) -> None:
        super().__init__()
        self.inner = inner
        self.secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        line = self.inner.format(record)
        for secret in self.secrets:
            line = line.replace(secret, "***")
        return line


class HumanErrorsFilter(logging.Filter):
    """Типовые сбои опроса Telegram — понятной фразой и не чаще раза в минуту (008 §7): aiogram
    повторяет запрос каждые несколько секунд и пишет на каждый повтор строку на английском."""

    def __init__(
        self, every_s: float = HUMAN_ERRORS_EVERY_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__()
        self.every_s = every_s
        self.clock = clock
        self.last: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage().startswith("Sleep for "):
            # «Sleep for N seconds and try again» после каждой неудачи: понятная строка уже была
            now = self.clock()
            return not any(now - t < self.every_s for t in self.last.values())
        if not record.getMessage().startswith("Failed to fetch updates"):
            return True
        kind = next((k for k in HUMAN_ERRORS if k in record.getMessage()), None)
        if kind is None:
            return True
        now = self.clock()
        if now - self.last.get(kind, -self.every_s) < self.every_s:
            return False
        self.last[kind] = now
        record.msg, record.args = HUMAN_ERRORS[kind], None
        record.error = kind
        return True


def configure_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(JsonFormatter(), secrets))
    handler.addFilter(HumanErrorsFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # запросы к Telegram и API на уровне DEBUG содержат URL с токеном и тела сообщений
    for name in ("aiogram.client", "aiohttp", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLevelName(level)))
