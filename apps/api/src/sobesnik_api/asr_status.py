"""Готова ли модель распознавания (011 §4): воркер пишет в Redis, API читает при приёме голоса.

Ключа нет — воркер ещё не стартовал, модель считается неготовой. Ошибка Redis — тоже «не готова»:
худшее, что случится, — человек увидит «первый раз это пара минут» при уже готовой модели.
"""

import logging
from typing import Literal, Protocol

from redis.asyncio import Redis

log = logging.getLogger(__name__)

ASRState = Literal["downloading", "ready", "failed"]

KEY_PREFIX = "sobesnik:asr-model:"


class ASRStatus(Protocol):
    async def get(self, model: str) -> str | None: ...

    async def set(self, model: str, state: ASRState) -> None: ...

    async def aclose(self) -> None: ...


async def asr_ready(status: ASRStatus, model: str) -> bool:
    return await status.get(model) == "ready"


class MemoryASRStatus:
    """Для тестов и для воркера без Redis."""

    def __init__(self, states: dict[str, str] | None = None) -> None:
        self.states: dict[str, str] = dict(states or {})

    async def get(self, model: str) -> str | None:
        return self.states.get(model)

    async def set(self, model: str, state: ASRState) -> None:
        self.states[model] = state

    async def aclose(self) -> None:
        pass


class RedisASRStatus:
    """Best effort: ошибка Redis пишется в лог и не валит ни приём ответа, ни воркер."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @classmethod
    def from_url(cls, url: str) -> "RedisASRStatus":
        return cls(Redis.from_url(url, decode_responses=True, socket_timeout=1))

    async def get(self, model: str) -> str | None:
        try:
            value = await self._redis.get(KEY_PREFIX + model)
        except Exception as exc:
            log.warning("asr status not read", extra={"error": type(exc).__name__})
            return None
        return str(value) if value is not None else None

    async def set(self, model: str, state: ASRState) -> None:
        try:
            await self._redis.set(KEY_PREFIX + model, state)
        except Exception as exc:
            log.warning("asr status not saved", extra={"error": type(exc).__name__})

    async def aclose(self) -> None:
        await self._redis.aclose()
