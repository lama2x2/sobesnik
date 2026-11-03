"""Очередь задач воркера (003 §8.1, 004 §8). API ставит задачи через интерфейс, тесты
подменяют его."""

import uuid
from typing import Protocol

from arq.connections import ArqRedis, RedisSettings, create_pool

EVALUATE_JOB = "evaluate_answer_job"
TRANSCRIBE_JOB = "transcribe_answer_job"
RUN_JOB = "run_eval_job"
KEY_POINTS_JOB = "check_key_points_job"


def evaluation_job_id(answer_id: uuid.UUID) -> str:
    return f"evaluate:{answer_id}"


def transcription_job_id(answer_id: uuid.UUID) -> str:
    return f"transcribe:{answer_id}"


def run_job_id(run_id: uuid.UUID) -> str:
    return f"run:{run_id}"


def key_points_job_id(session_id: uuid.UUID) -> str:
    return f"key-points:{session_id}"


class TaskQueue(Protocol):
    async def enqueue_evaluation(self, answer_id: uuid.UUID) -> None: ...

    async def enqueue_transcription(self, answer_id: uuid.UUID) -> None: ...

    async def enqueue_run(self, run_id: uuid.UUID) -> None: ...

    async def enqueue_key_points_check(self, session_id: uuid.UUID) -> None: ...

    async def aclose(self) -> None: ...


def redis_settings(url: str, *, retries: int = 5) -> RedisSettings:
    settings = RedisSettings.from_dsn(url)
    settings.conn_retries = retries
    return settings


class ArqQueue:
    """Очередь arq. Соединение открывается при первой задаче, чтобы API стартовал без Redis.

    Одинаковый `job_id` не ставится дважды, пока задача в очереди или выполняется.
    """

    def __init__(self, url: str, *, pool: ArqRedis | None = None) -> None:
        self._url = url
        self._pool = pool
        self._own_pool = pool is None

    async def _redis(self) -> ArqRedis:
        if self._pool is None:
            # без повторов: приём ответа не должен ждать недоступный Redis
            self._pool = await create_pool(redis_settings(self._url, retries=0))
        return self._pool

    async def enqueue_evaluation(self, answer_id: uuid.UUID) -> None:
        redis = await self._redis()
        await redis.enqueue_job(EVALUATE_JOB, str(answer_id), _job_id=evaluation_job_id(answer_id))

    async def enqueue_transcription(self, answer_id: uuid.UUID) -> None:
        redis = await self._redis()
        await redis.enqueue_job(
            TRANSCRIBE_JOB, str(answer_id), _job_id=transcription_job_id(answer_id)
        )

    async def enqueue_run(self, run_id: uuid.UUID) -> None:
        redis = await self._redis()
        await redis.enqueue_job(RUN_JOB, str(run_id), _job_id=run_job_id(run_id))

    async def enqueue_key_points_check(self, session_id: uuid.UUID) -> None:
        redis = await self._redis()
        await redis.enqueue_job(
            KEY_POINTS_JOB, str(session_id), _job_id=key_points_job_id(session_id)
        )

    async def aclose(self) -> None:
        if self._pool is not None and self._own_pool:
            await self._pool.aclose()
        self._pool = None
