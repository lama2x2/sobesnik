"""Состояние бота в Redis (006 §7.3): только идентификаторы, без текстов ответов и аудио."""

import json
import uuid
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

from redis.asyncio import Redis

Mode = Literal["idle", "answer", "waiting"]
"""`answer` — ждём ответа на вопрос, `waiting` — ответ принят, ждём оценку."""

PENDING_TTL_S = 24 * 60 * 60
"""Ожидающий ответ, о котором бот так и не узнал результат, забывается через сутки."""


def _uuid(value: Any) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


@dataclass
class UserState:
    user_id: uuid.UUID | None = None
    vacancy_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    question_id: uuid.UUID | None = None
    position: int = 0
    """Позиция текущего вопроса в сессии; следующий вопрос — первый после неё без ответа."""
    mode: Mode = "idle"
    noted_session: uuid.UUID | None = None
    """Сессия, в которой уже показана оговорка «балл примерный, ±20» (006 §8.2)."""


@dataclass
class Pending:
    """Ответ, результат которого бот ждёт событием (006 §6.3)."""

    answer_id: uuid.UUID
    telegram_id: int
    chat_id: int
    message_id: int
    """Статусное сообщение, которое редактируется по событиям."""
    kind: str
    session_id: uuid.UUID
    question_id: uuid.UUID
    accepted_at: float
    last_status: str = "received"
    slow_noted: bool = False


def _dump(obj: Any) -> str:
    return json.dumps(
        {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in asdict(obj).items()}
    )


class StateStore:
    def __init__(self, redis: Redis, prefix: str = "bot") -> None:
        self._redis = redis
        self._prefix = prefix

    def _user_key(self, telegram_id: int) -> str:
        return f"{self._prefix}:user:{telegram_id}"

    def _pending_key(self, answer_id: uuid.UUID) -> str:
        return f"{self._prefix}:pending:{answer_id}"

    @property
    def _pending_index(self) -> str:
        return f"{self._prefix}:pending"

    async def get_user(self, telegram_id: int) -> UserState:
        raw = await self._redis.get(self._user_key(telegram_id))
        if raw is None:
            return UserState()
        data = json.loads(raw)
        return UserState(
            user_id=_uuid(data.get("user_id")),
            vacancy_id=_uuid(data.get("vacancy_id")),
            session_id=_uuid(data.get("session_id")),
            question_id=_uuid(data.get("question_id")),
            position=int(data.get("position", 0)),
            mode=data.get("mode", "idle"),
            noted_session=_uuid(data.get("noted_session")),
        )

    async def save_user(self, telegram_id: int, state: UserState) -> None:
        await self._redis.set(self._user_key(telegram_id), _dump(state))

    async def add_pending(self, pending: Pending) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._pending_key(pending.answer_id), _dump(pending), ex=PENDING_TTL_S)
            pipe.sadd(self._pending_index, str(pending.answer_id))
            await pipe.execute()

    save_pending = add_pending

    async def get_pending(self, answer_id: uuid.UUID) -> Pending | None:
        raw = await self._redis.get(self._pending_key(answer_id))
        if raw is None:
            return None
        data = json.loads(raw)
        known = {f.name for f in fields(Pending)}
        values = {k: v for k, v in data.items() if k in known}
        for key in ("answer_id", "session_id", "question_id"):
            values[key] = uuid.UUID(values[key])
        return Pending(**values)

    async def remove_pending(self, answer_id: uuid.UUID) -> bool:
        """`True`, если ответ ещё ждал: так результат обрабатывается ровно один раз."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._pending_key(answer_id))
            pipe.srem(self._pending_index, str(answer_id))
            deleted, _ = await pipe.execute()
        return bool(deleted)

    async def all_pending(self) -> list[Pending]:
        result = []
        for raw_id in await self._redis.smembers(self._pending_index):  # type: ignore[misc]
            answer_id = uuid.UUID(raw_id if isinstance(raw_id, str) else raw_id.decode())
            pending = await self.get_pending(answer_id)
            if pending is None:  # истёк TTL
                await self._redis.srem(self._pending_index, str(answer_id))  # type: ignore[misc]
            else:
                result.append(pending)
        return result
