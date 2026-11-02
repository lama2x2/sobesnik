"""Отображение оценки, чтение событий из Redis, логи без токена, клиент API (006 §6.3, §7.5, §8)."""

import json
import logging
import uuid

import httpx
import pytest
from redis.asyncio import Redis

from sobesnik_bot import render
from sobesnik_bot.api import Answer, ApiClient, ApiError, ApiUnavailable, Evaluation, Transcript
from sobesnik_bot.events import GROUP, EventConsumer
from sobesnik_bot.logs import JsonFormatter, RedactingFormatter

from .conftest import TOKEN


@pytest.mark.parametrize(
    ("total", "passed", "word"),
    [
        (90, True, "🟢 Хорошо"),
        (75, True, "🟢 Хорошо"),
        (74, True, "🟡 Средне"),
        (80, False, "🔴 Слабо"),
    ],
)
def test_band(total: int, passed: bool, word: str) -> None:
    assert render.band(total, passed) == word


def test_approx_and_dots() -> None:
    assert [render.approx(t) for t in (0, 62, 63, 98, 100)] == [0, 60, 65, 100, 100]
    assert [render.dots(s) for s in (0, 2, 3, 5)] == ["○○○", "●●○", "●●●", "●●●"]


def _answer(transcript: str | None = None, doubts: list[str] | None = None) -> Answer:
    return Answer(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        question_id=uuid.uuid4(),
        kind="voice" if transcript else "text",
        status="done",
        error=None,
        transcript=Transcript(text=transcript) if transcript else None,
        evaluation=Evaluation(
            total=43,
            passed=False,
            scores=[],
            missed_points=["<b>пункт</b>"],
            advice="Совет & пример",
            asr_doubts=doubts or [],
        ),
    )


def test_evaluation_text_escapes_and_trims() -> None:
    text = render.evaluation_text(_answer(), note=False)
    assert text.startswith("<b>🔴 Слабо</b> · ≈ 45 из 100")
    assert "• &lt;b&gt;пункт&lt;/b&gt;" in text
    assert "Совет & пример" not in text and "Совет &amp; пример" in text
    assert "Распознано" not in text

    long = "слово " * 1000
    voice = render.evaluation_text(_answer(long, ["«a<b» — возможно, «c»"]), note=True)
    assert "Балл примерный, ±20" in voice
    quoted = voice.split("<blockquote expandable>")[1].split("</blockquote>")[0]
    assert len(quoted) <= render.TRANSCRIPT_MAX_CHARS and quoted.endswith("…")
    assert "«a&lt;b»" in voice
    assert len(voice) < 4096


def test_every_worker_error_has_text() -> None:
    worker_errors = {
        "asr_empty", "audio_unreadable", "audio_missing", "asr_failed", "no_transcript",
        "llm_unavailable", "llm_bad_output", "llm_error",
    }  # fmt: skip
    assert worker_errors <= set(render.FAILURES)
    assert "код: weird" in render.failure_text("weird")


def test_redacting_formatter() -> None:
    formatter = RedactingFormatter(JsonFormatter(), [TOKEN])
    record = logging.makeLogRecord(
        {"msg": f"POST https://api.telegram.org/bot{TOKEN}/getUpdates", "url": f"/bot{TOKEN}/x"}
    )
    line = formatter.format(record)
    assert TOKEN not in line
    assert json.loads(line)["msg"] == "POST https://api.telegram.org/bot***/getUpdates"


async def test_event_consumer(redis: Redis) -> None:
    seen: list[tuple[uuid.UUID, str]] = []
    first = uuid.uuid4()

    async def handler(answer_id: uuid.UUID, status: str) -> None:
        if status == "boom":
            raise RuntimeError("сбой обработки")
        seen.append((answer_id, status))

    consumer = EventConsumer(redis, "test:events", handler, consumer="c1")
    await consumer.ensure_group()
    await consumer.ensure_group()  # группа уже есть — не ошибка
    await redis.xadd("test:events", {"answer_id": str(first), "status": "evaluating"})
    await redis.xadd("test:events", {"answer_id": str(first), "status": "boom"})
    assert await consumer.read(block_ms=10) == 2
    assert seen == [(first, "evaluating")]
    # всё подтверждено, даже упавшее: ответ подхватит сверка
    assert (await redis.xpending("test:events", GROUP))["pending"] == 0


async def test_event_consumer_reads_unacked_after_restart(redis: Redis) -> None:
    """Событие прочитано, но бот упал до XACK — после перезапуска оно читается снова."""
    answer_id = uuid.uuid4()
    seen: list[str] = []

    async def handler(_: uuid.UUID, status: str) -> None:
        seen.append(status)

    consumer = EventConsumer(redis, "test:events", handler, consumer="c1")
    await consumer.ensure_group()
    await redis.xadd("test:events", {"answer_id": str(answer_id), "status": "done"})
    await redis.xreadgroup(GROUP, "c1", {"test:events": ">"})  # «упал» до обработки
    assert await consumer.read(pending=True) == 1
    assert seen == ["done"]
    assert await consumer.read(pending=True) == 0


async def test_api_client_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer svc"
        if request.url.path.endswith("/users/telegram/1"):
            return httpx.Response(201, json={"id": str(uuid.uuid4()), "telegram_id": 1})
        if request.url.path.endswith("/vacancies"):
            body = {"error": {"code": "not_a_vacancy", "message": "Похоже на резюме."}}
            return httpx.Response(422, json=body)
        return httpx.Response(502, text="bad gateway")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = ApiClient("http://api/api/v1/", "svc", client=client)
    await api.upsert_user(1)
    with pytest.raises(ApiError) as err:
        await api.create_vacancy(uuid.uuid4(), text="резюме")
    assert (err.value.status, err.value.code) == (422, "not_a_vacancy")
    with pytest.raises(ApiUnavailable):
        await api.get_answer(uuid.uuid4())
    await api.aclose()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    offline = ApiClient("http://api", client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with pytest.raises(ApiUnavailable):
        await offline.upsert_user(1)
    await offline.aclose()
