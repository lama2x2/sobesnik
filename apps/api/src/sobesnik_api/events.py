"""События смены статуса ответа (ANS-3, 006 §6): воркер пишет в Redis Stream, бот читает.

В событии только идентификаторы и статус: текст ответа, транскрипт и оценку получатель берёт
из `GET /answers/{id}`. Публикуются только ответы пользователей — у ответов датасета нет получателя.
"""

import logging
from typing import Protocol

from redis.asyncio import Redis

from sobesnik_api.db.models import Answer

log = logging.getLogger(__name__)

STREAM_MAXLEN = 10_000
"""Сколько последних событий держит Stream; старые обрезаются приблизительно (`MAXLEN ~`)."""


def event_fields(answer: Answer) -> dict[str, str]:
    return {
        "answer_id": str(answer.id),
        "status": answer.status,
        "error": answer.error or "",
        "kind": answer.kind,
        "session_id": str(answer.session_id or ""),
        "user_id": str(answer.user_id or ""),
    }


class AnswerEvents(Protocol):
    async def publish(self, answer: Answer) -> None: ...

    async def aclose(self) -> None: ...


class NullAnswerEvents:
    async def publish(self, answer: Answer) -> None:
        pass

    async def aclose(self) -> None:
        pass


class RedisAnswerEvents:
    """Публикация best effort: ошибка Redis пишется в лог и не валит задачу (бот сверится сам)."""

    def __init__(self, redis: Redis, stream: str) -> None:
        self._redis = redis
        self._stream = stream

    @classmethod
    def from_url(cls, url: str, stream: str) -> "RedisAnswerEvents":
        return cls(Redis.from_url(url, decode_responses=True), stream)

    async def publish(self, answer: Answer) -> None:
        if answer.source != "user":
            return
        try:
            await self._redis.xadd(
                self._stream,
                event_fields(answer),  # type: ignore[arg-type]
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
        except Exception as exc:
            log.warning(
                "answer event not published",
                extra={"answer_id": str(answer.id), "error": type(exc).__name__},
            )

    async def aclose(self) -> None:
        await self._redis.aclose()
