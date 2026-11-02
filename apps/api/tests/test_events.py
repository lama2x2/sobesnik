"""События смены статуса ответа (006 §6): что публикует воркер и как они лежат в Redis Stream."""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from arq import Retry
from httpx import AsyncClient
from redis.asyncio import Redis

from sobesnik_api.db.models import Answer
from sobesnik_api.events import RedisAnswerEvents, event_fields
from sobesnik_api.worker import WorkerDeps, process_answer, process_transcription
from sobesnik_asr import FakeTranscriber
from sobesnik_asr.samples import ogg_opus_bytes
from sobesnik_llm import FakeProvider

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://redis:6379/15")


class RecordingEvents:
    def __init__(self, broken: bool = False) -> None:
        self.events: list[tuple[str, str, str | None]] = []
        self.broken = broken

    async def publish(self, answer: Answer) -> None:
        if self.broken:
            raise ConnectionError("redis недоступен")
        if answer.source == "user":
            self.events.append((str(answer.id), answer.status, answer.error))

    async def aclose(self) -> None:
        pass

    def statuses(self) -> list[tuple[str, str | None]]:
        return [(status, error) for _, status, error in self.events]


@pytest.fixture
def events() -> RecordingEvents:
    return RecordingEvents()


@pytest.fixture
def ev_deps(deps: WorkerDeps, events: RecordingEvents) -> WorkerDeps:
    return replace(deps, events=events)


async def text_answer(client: AsyncClient, session: dict[str, Any], position: int = 1) -> uuid.UUID:
    question = session["questions"][position - 1]
    response = await client.post(
        f"/sessions/{session['id']}/questions/{question['id']}/answer", json={"text": "ответ"}
    )
    assert response.status_code == 202
    return uuid.UUID(response.json()["answer_id"])


async def voice_answer(client: AsyncClient, session: dict[str, Any]) -> uuid.UUID:
    question = session["questions"][0]
    response = await client.post(
        f"/sessions/{session['id']}/questions/{question['id']}/answer/voice",
        files={"file": ("v.ogg", ogg_opus_bytes(2.0), "audio/ogg")},
    )
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["answer_id"])


async def test_text_answer_events(
    client: AsyncClient,
    session: dict[str, Any],
    ev_deps: WorkerDeps,
    events: RecordingEvents,
    llm: FakeProvider,
    replies: Any,
) -> None:
    answer_id = await text_answer(client, session)
    llm.replies = [replies.evaluation()]
    await process_answer(ev_deps, answer_id)
    assert events.events == [(str(answer_id), "evaluating", None), (str(answer_id), "done", None)]
    await process_answer(ev_deps, answer_id)  # повторная постановка — без новых событий
    assert len(events.events) == 2


async def test_voice_answer_events(
    client: AsyncClient,
    session: dict[str, Any],
    ev_deps: WorkerDeps,
    events: RecordingEvents,
    llm: FakeProvider,
    replies: Any,
) -> None:
    answer_id = await voice_answer(client, session)
    await process_transcription(ev_deps, answer_id)
    llm.replies = [replies.evaluation()]
    await process_answer(ev_deps, answer_id)
    # evaluating после распознавания и в начале оценки — одно событие: статус не менялся
    assert events.statuses() == [("transcribing", None), ("evaluating", None), ("done", None)]


async def test_failure_events(
    client: AsyncClient,
    session: dict[str, Any],
    ev_deps: WorkerDeps,
    events: RecordingEvents,
    llm: FakeProvider,
    transcriber: FakeTranscriber,
) -> None:
    transcriber.text = "  "
    silent = await voice_answer(client, session)
    await process_transcription(ev_deps, silent)
    assert events.statuses()[-1] == ("failed", "asr_empty")

    llm.replies = ["не json"] * 5
    bad = await text_answer(client, session, position=2)
    await process_answer(ev_deps, bad)
    assert events.statuses()[-1] == ("failed", "llm_bad_output")

    llm.available = False
    down = await text_answer(client, session, position=3)
    with pytest.raises(Retry):
        await process_answer(ev_deps, down, job_try=1)
    assert events.statuses()[-1] == ("evaluating", None)
    await process_answer(ev_deps, down, job_try=ev_deps.settings.eval_max_tries)
    assert events.statuses()[-1] == ("failed", "llm_unavailable")


async def test_publish_failure_does_not_break_job(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
) -> None:
    broken = RedisAnswerEvents(Redis.from_url("redis://127.0.0.1:1/0"), "s")
    answer_id = await text_answer(client, session)
    llm.replies = [replies.evaluation()]
    await process_answer(replace(deps, events=broken), answer_id)
    async with deps.sessionmaker() as db:
        answer = await db.get(Answer, answer_id)
    assert answer is not None and answer.status == "done"
    await broken.aclose()


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def test_redis_stream(redis: Redis) -> None:
    events = RedisAnswerEvents(redis, "test:events")
    user = Answer(
        id=uuid.uuid4(),
        question_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        source="user",
        kind="voice",
        status="evaluating",
    )
    dataset = Answer(
        id=uuid.uuid4(), question_id=uuid.uuid4(), source="dataset", kind="text", status="imported"
    )
    await events.publish(user)
    await events.publish(dataset)  # ответы датасета не публикуются
    entries = await redis.xrange("test:events")
    assert [fields for _, fields in entries] == [event_fields(user)]
    assert entries[0][1]["error"] == ""
    assert "text" not in entries[0][1]
