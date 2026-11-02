"""Чтение событий статуса ответа из Redis Stream (006 §6.3).

Группа потребителей хранит непрочитанное, пока бот перезапускается: после старта он сначала
дочитывает свои неподтверждённые события, потом ждёт новые.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

log = logging.getLogger(__name__)

GROUP = "bot"
BLOCK_MS = 5_000

Handler = Callable[[uuid.UUID, str], Awaitable[None]]


class EventConsumer:
    def __init__(
        self, redis: Redis, stream: str, handler: Handler, *, consumer: str = "bot-1"
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._handler = handler
        self._consumer = consumer

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(self._stream, GROUP, id="$", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(self, *, pending: bool = False, block_ms: int | None = BLOCK_MS) -> int:
        """Читает и обрабатывает пачку: `pending` — свои неподтверждённые, иначе новые.

        Ошибка обработчика пишется в лог, событие всё равно подтверждается: ответ остаётся
        ожидающим, и сверка (`Trainer.reconcile`) подхватит его сама.
        """
        streams: dict[Any, Any] = {self._stream: "0" if pending else ">"}
        result = await self._redis.xreadgroup(
            GROUP, self._consumer, streams, count=50, block=None if pending else block_ms
        )
        count = 0
        for _, entries in result or []:
            for entry_id, fields in entries:
                count += 1
                try:
                    await self._handler(uuid.UUID(_s(fields["answer_id"])), _s(fields["status"]))
                except Exception:
                    log.exception("answer event failed", extra={"entry": _s(entry_id)})
                await self._redis.xack(self._stream, GROUP, entry_id)
        return count

    async def run(self) -> None:
        await self.ensure_group()
        while await self.read(pending=True):
            pass
        while True:
            await self.read()


def _s(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value
