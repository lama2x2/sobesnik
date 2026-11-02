"""JSON-логи бота в stdout (006 §7.5): без текстов сообщений и аудио, токен бота вырезается."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

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


def configure_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(JsonFormatter(), secrets))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # запросы к Telegram и API на уровне DEBUG содержат URL с токеном и тела сообщений
    for name in ("aiogram.client", "aiohttp", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLevelName(level)))
