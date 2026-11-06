"""Отображение оценки, чтение событий из Redis, логи без токена, клиент API (006 §6.3, §7.5, §8)."""

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import User
from redis.asyncio import Redis

from sobesnik_bot import render
from sobesnik_bot.__main__ import (
    DNS_CACHE_S,
    TOKEN_REJECTED,
    connect,
    llm_summary,
    telegram_session,
)
from sobesnik_bot.api import Answer, ApiClient, ApiError, ApiUnavailable, Evaluation, Transcript
from sobesnik_bot.events import GROUP, EventConsumer
from sobesnik_bot.logs import (
    NO_TELEGRAM,
    TOKEN_CONFLICT,
    HumanErrorsFilter,
    JsonFormatter,
    RedactingFormatter,
)

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


def fetch_failed(kind: str) -> logging.LogRecord:
    return logging.makeLogRecord(
        {
            "name": "aiogram.dispatcher",
            "msg": "Failed to fetch updates - %s: %s",
            "args": (kind, "Conflict: terminated by other getUpdates request"),
        }
    )


def test_human_errors_filter() -> None:
    """Сбои опроса Telegram — понятной фразой и не чаще раза в минуту (008 §7)."""
    now = [0.0]
    filt = HumanErrorsFilter(every_s=60, clock=lambda: now[0])
    record = fetch_failed("TelegramConflictError")
    assert filt.filter(record)
    assert record.getMessage() == TOKEN_CONFLICT
    now[0] = 5
    assert not filt.filter(fetch_failed("TelegramConflictError"))
    assert filt.filter(fetch_failed("TelegramNetworkError"))  # другой вид — своё окно
    now[0] = 61
    assert filt.filter(fetch_failed("TelegramConflictError"))
    sleep = logging.makeLogRecord({"msg": "Sleep for 2.5 seconds and try again... (tryings = 3)"})
    assert not filt.filter(sleep)  # повтор после понятной строки — шум
    now[0] = 200
    assert filt.filter(sleep)  # давно не было сбоев — пусть видно
    other = logging.makeLogRecord({"msg": "Failed to fetch updates - ValueError: x"})
    assert filt.filter(other)
    assert filt.filter(logging.makeLogRecord({"msg": "bot started"}))


class ScriptedSession(BaseSession):
    """`getMe`: по очереди бросает исключения из `script`, потом отвечает ботом."""

    def __init__(self, script: list[Exception]) -> None:
        super().__init__()
        self.script = script

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура aiogram
    ) -> TelegramType:
        assert isinstance(method, GetMe)
        if self.script:
            raise self.script.pop(0)
        return User(id=1, is_bot=True, first_name="Sobesnik", username="sobesnik_bot")  # type: ignore[return-value]

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        pass


async def test_connect_rejected_token(caplog: pytest.LogCaptureFixture) -> None:
    session = ScriptedSession([TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")])
    with caplog.at_level(logging.ERROR):
        assert await connect(Bot(TOKEN, session=session), retry_s=0) is None
    assert TOKEN_REJECTED in caplog.text


async def test_connect_retries_without_network(caplog: pytest.LogCaptureFixture) -> None:
    session = ScriptedSession([TelegramNetworkError(method=GetMe(), message="Cannot connect")] * 2)
    with caplog.at_level(logging.WARNING):
        me = await connect(Bot(TOKEN, session=session), retry_s=0)
    assert me is not None and me.username == "sobesnik_bot"
    assert caplog.text.count(NO_TELEGRAM) == 2


async def test_session_rereads_telegram_address_after_vpn_switch() -> None:
    session = telegram_session()
    try:
        client = await session.create_session()
        assert client.connector._cached_hosts._ttl == DNS_CACHE_S  # type: ignore[union-attr]
    finally:
        await session.close()


async def test_llm_status_and_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/health/llm"
        bad = {"ok": False, "model": "qwen3.5:4b", "hint": "ollama pull qwen3.5:4b"}
        return httpx.Response(
            503, json={"status": "unavailable", "roles": {"gen": bad, "eval": bad}}
        )

    api = ApiClient(
        "http://api/api/v1", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    status = await api.llm_status()
    assert (status.ok, status.hint) == (False, "ollama pull qwen3.5:4b")
    assert await llm_summary(api) == "ollama pull qwen3.5:4b"
    await api.aclose()

    def ok(_: httpx.Request) -> httpx.Response:
        good = {"ok": True, "model": "qwen3.5:4b", "hint": None}
        return httpx.Response(200, json={"status": "ok", "roles": {"gen": good, "eval": good}})

    api = ApiClient(
        "http://api/api/v1", client=httpx.AsyncClient(transport=httpx.MockTransport(ok))
    )
    assert await llm_summary(api) == "модель qwen3.5:4b готова"
    await api.aclose()


async def test_api_error_carries_hint() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        body = {"error": {"code": "llm_model_missing", "message": "нет", "hint": "ollama pull x"}}
        return httpx.Response(503, json=body)

    api = ApiClient("http://api", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ApiError) as err:
        await api.upsert_user(1)
    assert (err.value.code, err.value.hint) == ("llm_model_missing", "ollama pull x")
    await api.aclose()
