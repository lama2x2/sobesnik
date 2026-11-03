"""Тесты бота без Telegram и без API: Telegram — фейковая сессия `Bot`, которая записывает вызовы,
API — `FakeApi` через протокол `Api`. Состояние — Redis из compose (отдельная база)."""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import EditMessageText, GetFile, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, File, Message
from redis.asyncio import Redis

from sobesnik_bot.__main__ import build_dispatcher
from sobesnik_bot.api import (
    Accepted,
    Answer,
    ApiError,
    CriterionScore,
    Doc,
    Evaluation,
    Forgotten,
    Level,
    LlmStatus,
    Progress,
    Requirement,
    Session,
    SessionAnswer,
    SessionQuestion,
    Summary,
    SummaryQuestion,
    Transcript,
    User,
    Vacancy,
    WebLink,
)
from sobesnik_bot.flow import Trainer
from sobesnik_bot.state import StateStore

TEST_REDIS_URL = os.environ.get("TEST_BOT_REDIS_URL", "redis://redis:6379/14")
TOKEN = "42:TEST-token-abcdefghijklmnopqrstuvwxyz0123"
CHAT = USER = 1001
CRITERIA = [
    ("correctness", "Корректность"),
    ("completeness", "Полнота"),
    ("depth", "Глубина"),
    ("terminology", "Терминология"),
    ("structure", "Структура"),
]


class FakeSession(BaseSession):
    """Сессия Telegram: запоминает методы, отдаёт правдоподобные ответы, файлы — из `files`."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.files: dict[str, bytes] = {}
        self.sent: list[Message] = []
        self.fail_next = 0
        """Сколько следующих запросов упадёт с сетевой ошибкой: Telegram недоступен."""
        """Сообщения, которые вернул `sendMessage`: по ним видны `message_id`."""
        self._ids = count(500)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура aiogram
    ) -> TelegramType:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TelegramNetworkError(method=method, message="Cannot connect to host")
        self.calls.append(method)
        result: Any = True
        if isinstance(method, SendMessage):
            result = Message(
                message_id=next(self._ids),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
            self.sent.append(result)
        elif isinstance(method, GetFile):
            result = File(
                file_id=method.file_id, file_unique_id="u", file_path=f"files/{method.file_id}"
            )
        return result  # type: ignore[no-any-return]

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 — сигнатура aiogram
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield self.files[url.rsplit("/", 1)[-1]]

    async def close(self) -> None:
        pass

    # --- выборки для проверок

    def of(self, kind: type[TelegramMethod[Any]]) -> list[Any]:
        return [c for c in self.calls if isinstance(c, kind)]

    def texts(self) -> list[str]:
        """Тексты отправленных и отредактированных сообщений по порядку."""
        return [str(c.text) for c in self.calls if isinstance(c, SendMessage | EditMessageText)]

    def last_text(self) -> str:
        return self.texts()[-1]

    def clear(self) -> None:
        self.calls.clear()
        self.sent.clear()


def _question(position: int) -> SessionQuestion:
    return SessionQuestion(
        id=uuid.uuid4(),
        position=position,
        text=f"Вопрос {position}: зачем нужен <index>?",
        topic=f"db.topic{position}",
    )


class FakeApi:
    """API в памяти: одна вакансия, одна сессия, ответы меняются методами `finish_*`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, ApiError] = {}
        self.user_id = uuid.uuid4()
        self.vacancy = Vacancy(
            id=uuid.uuid4(),
            title="Python-разработчик",
            level="middle",
            stack=["python", "postgresql"],
            requirements=[
                Requirement(id=uuid.uuid4(), text="Asyncio", is_required=True, topic=None),
                Requirement(id=uuid.uuid4(), text="Docker", is_required=False, topic=None),
            ],
        )
        self.progress = Progress(topics=[], sessions=[])
        self.session: Session | None = None
        self.answers: dict[uuid.UUID, Answer] = {}
        self.voice_data: list[tuple[bytes, str, str]] = []
        self.closed = False
        self.gate: asyncio.Event | None = None
        """Если задан, разбор вакансии ждёт его — как долгий вызов модели."""
        self.llm = LlmStatus(ok=True, model="qwen3.5:4b")
        """Ответ `llm_status`; в `calls` не пишется — его спрашивают попутно, на /start."""

    def _call(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        if name in self.errors:
            raise self.errors.pop(name)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def llm_status(self) -> LlmStatus:
        if "llm_status" in self.errors:
            raise self.errors.pop("llm_status")
        return self.llm

    async def upsert_user(self, telegram_id: int) -> User:
        self._call("upsert_user", telegram_id=telegram_id)
        return User(id=self.user_id)

    async def create_vacancy(
        self,
        user_id: uuid.UUID,
        *,
        text: str | None = None,
        url: str | None = None,
        html: str | None = None,
    ) -> Vacancy:
        self._call("create_vacancy", user_id=user_id, text=text, url=url, html=html)
        if self.gate is not None:
            await self.gate.wait()
        return self.vacancy

    async def set_level(self, vacancy_id: uuid.UUID, level: Level) -> Vacancy:
        self._call("set_level", vacancy_id=vacancy_id, level=level)
        self.vacancy = self.vacancy.model_copy(update={"level": level})
        return self.vacancy

    async def get_vacancy(self, vacancy_id: uuid.UUID) -> Vacancy:
        self._call("get_vacancy", vacancy_id=vacancy_id)
        return self.vacancy

    async def create_session(
        self, vacancy_id: uuid.UUID, n: int, topics: list[str] | None = None
    ) -> Session:
        if topics is None:
            self._call("create_session", vacancy_id=vacancy_id, n=n)
        else:
            self._call("create_session", vacancy_id=vacancy_id, n=n, topics=topics)
        return self._new_session(vacancy_id, n, "vacancy")

    def _new_session(self, vacancy_id: uuid.UUID | None, n: int, mode: str) -> Session:
        self.session = Session(
            id=uuid.uuid4(),
            vacancy_id=vacancy_id,
            mode=mode,
            status="active",
            questions=[_question(i + 1) for i in range(n)],
        )
        self.answers.clear()
        return self.session

    async def create_repeat(self, user_id: uuid.UUID, n: int) -> Session:
        self._call("create_repeat", user_id=user_id, n=n)
        return self._new_session(None, 2, "repeat")

    async def get_progress(self, user_id: uuid.UUID) -> Progress:
        self._call("get_progress", user_id=user_id)
        return self.progress

    def _latest(self, question_id: uuid.UUID) -> Answer | None:
        found = [a for a in self.answers.values() if a.question_id == question_id]
        return found[-1] if found else None

    async def get_session(self, session_id: uuid.UUID) -> Session:
        self._call("get_session", session_id=session_id)
        assert self.session is not None and self.session.id == session_id
        questions = []
        for q in self.session.questions:
            a = self._latest(q.id)
            ev = a.evaluation if a else None
            answer = (
                SessionAnswer(
                    id=a.id,
                    kind=a.kind,
                    status=a.status,
                    total=ev.total if ev else None,
                    passed=ev.passed if ev else None,
                )
                if a
                else None
            )
            questions.append(q.model_copy(update={"answer": answer}))
        return self.session.model_copy(update={"questions": questions})

    async def finish_session(self, session_id: uuid.UUID) -> Summary:
        self._call("finish_session", session_id=session_id)
        assert self.session is not None
        self.session = self.session.model_copy(update={"status": "finished"})
        rows = []
        for q in self.session.questions:
            a = self._latest(q.id)
            ev = a.evaluation if a else None
            rows.append(
                SummaryQuestion(
                    position=q.position,
                    question_id=q.id,
                    answer_status=a.status if a else None,
                    total=ev.total if ev else None,
                    passed=ev.passed if ev else None,
                )
            )
        return Summary(
            questions=rows,
            answered=sum(r.answer_status is not None for r in rows),
            evaluated=sum(r.total is not None for r in rows),
            passed=sum(bool(r.passed) for r in rows),
        )

    def _accept(self, session_id: uuid.UUID, question_id: uuid.UUID, kind: str) -> Accepted:
        answer = Answer(
            id=uuid.uuid4(),
            session_id=session_id,
            question_id=question_id,
            kind=kind,
            status="received",
            error=None,
        )
        self.answers[answer.id] = answer
        return Accepted(answer_id=answer.id, status="received")

    async def answer_text(
        self, session_id: uuid.UUID, question_id: uuid.UUID, text: str
    ) -> Accepted:
        self._call("answer_text", question_id=question_id, text=text)
        return self._accept(session_id, question_id, "text")

    async def answer_voice(
        self,
        session_id: uuid.UUID,
        question_id: uuid.UUID,
        data: bytes,
        filename: str,
        mime: str,
    ) -> Accepted:
        self._call("answer_voice", question_id=question_id, filename=filename, mime=mime)
        self.voice_data.append((data, filename, mime))
        return self._accept(session_id, question_id, "voice")

    async def get_answer(self, answer_id: uuid.UUID) -> Answer:
        self._call("get_answer", answer_id=answer_id)
        return self.answers[answer_id]

    async def forget_audio(self, user_id: uuid.UUID) -> Forgotten:
        self._call("forget_audio", user_id=user_id)
        return Forgotten(audio_deleted=2, transcripts_deleted=2)

    async def forget_all(self, user_id: uuid.UUID) -> Forgotten:
        self._call("forget_all", user_id=user_id)
        return Forgotten(
            audio_deleted=2,
            transcripts_deleted=2,
            answers_deleted=7,
            sessions_deleted=2,
            vacancies_deleted=1,
        )

    async def web_link(self, user_id: uuid.UUID) -> WebLink:
        self._call("web_link", user_id=user_id)
        return WebLink(secret="s3cr3t-link", code="K7MQ-2XDA", expires_at=datetime.now(UTC))

    async def aclose(self) -> None:
        self.closed = True

    # --- что делает воркер

    def set_status(
        self, answer_id: uuid.UUID, status: str, *, error: str | None = None, transcript: str = ""
    ) -> None:
        update: dict[str, Any] = {"status": status, "error": error}
        if transcript:
            update["transcript"] = Transcript(text=transcript)
        self.answers[answer_id] = self.answers[answer_id].model_copy(update=update)

    def evaluate(
        self,
        answer_id: uuid.UUID,
        total: int = 67,
        passed: bool = True,
        doubts: list[str] | None = None,
        docs: list[Doc] | None = None,
    ) -> None:
        evaluation = Evaluation(
            total=total,
            passed=passed,
            scores=[
                CriterionScore(criterion=cid, title=title, score=2, reason=f"почему {cid}")
                for cid, title in CRITERIA
            ],
            missed_points=["GIL не мешает I/O"],
            advice="Начните с определения.",
            asr_doubts=doubts or [],
        )
        self.answers[answer_id] = self.answers[answer_id].model_copy(
            update={"status": "done", "evaluation": evaluation, "docs": docs or []}
        )

    def last_answer(self) -> Answer:
        return list(self.answers.values())[-1]


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def tg() -> FakeSession:
    return FakeSession()


@pytest.fixture
async def bot(tg: FakeSession) -> Bot:
    return Bot(TOKEN, session=tg, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(redis: Redis) -> StateStore:
    return StateStore(redis, prefix="test")


@pytest.fixture
async def trainer(
    bot: Bot, api: FakeApi, store: StateStore, clock: Clock
) -> AsyncIterator[Trainer]:
    trainer = Trainer(
        bot, api, store, questions=3, now=clock, glue_wait_s=0.01, send_retry_delays_s=(0, 0)
    )
    yield trainer
    await trainer.close()


class Chat_:
    """Пользователь в Telegram: шлёт апдейты через диспетчер, как настоящий long polling."""

    def __init__(
        self, dp: Dispatcher, bot: Bot, user_id: int = USER, chat_type: str = "private"
    ) -> None:
        self.dp = dp
        self.bot = bot
        self.user_id = user_id
        self.chat_type = chat_type
        self._ids = count(1)

    def _message(self, **fields: Any) -> dict[str, Any]:
        return {
            "message_id": next(self._ids),
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": self.user_id, "type": self.chat_type},
            "from": {"id": self.user_id, "is_bot": False, "first_name": "Кандидат"},
            **fields,
        }

    async def _feed(self, update: dict[str, Any]) -> None:
        update["update_id"] = next(self._ids)
        await self.dp.feed_raw_update(self.bot, update)

    async def send(self, text: str, entities: list[dict[str, Any]] | None = None) -> None:
        if text.startswith("/"):
            entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        fields: dict[str, Any] = {"text": text}
        if entities:
            fields["entities"] = entities
        await self._feed({"message": self._message(**fields)})

    async def photo(self, caption: str) -> None:
        photo = [{"file_id": "p", "file_unique_id": "up", "width": 10, "height": 10}]
        await self._feed({"message": self._message(photo=photo, caption=caption)})

    async def voice(self, file_id: str = "voice-1", size: int = 3000) -> None:
        voice = {
            "file_id": file_id,
            "file_unique_id": "uv",
            "duration": 30,
            "mime_type": "audio/ogg",
            "file_size": size,
        }
        await self._feed({"message": self._message(voice=voice)})

    async def video_note(self, file_id: str = "circle-1", size: int = 300_000) -> None:
        note = {"file_id": file_id, "file_unique_id": "uc", "length": 384, "duration": 20}
        note["file_size"] = size
        await self._feed({"message": self._message(video_note=note)})

    async def document(
        self, name: str, file_id: str = "doc-1", size: int = 100, mime: str | None = None
    ) -> None:
        doc = {"file_id": file_id, "file_unique_id": "ud", "file_name": name, "file_size": size}
        if mime:
            doc["mime_type"] = mime
        await self._feed({"message": self._message(document=doc)})

    async def press(self, data: str, message_id: int = 77) -> None:
        query = {
            "id": str(next(self._ids)),
            "from": {"id": self.user_id, "is_bot": False, "first_name": "Кандидат"},
            "chat_instance": "ci",
            "data": data,
            "message": self._message(message_id=message_id, text="кнопки"),
        }
        await self._feed({"callback_query": query})


@pytest.fixture
def dp(trainer: Trainer) -> Dispatcher:
    return build_dispatcher(trainer, frozenset())


@pytest.fixture
def user(dp: Dispatcher, bot: Bot) -> Chat_:
    return Chat_(dp, bot)
