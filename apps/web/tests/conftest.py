"""Тесты веба: настоящий FastAPI-веб и фейковый API в памяти (через протоколы `Gate` и `Api`)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest
from httpx import ASGITransport, AsyncClient

from sobesnik_web.api import (
    Accepted,
    Answer,
    ApiError,
    Difficulty,
    Doc,
    Forgotten,
    Level,
    LlmStatus,
    Me,
    Progress,
    Requirement,
    Review,
    ReviewMistake,
    ReviewPoint,
    Session,
    SessionAnswer,
    SessionQuestion,
    Summary,
    SummaryQuestion,
    Token,
    Vacancy,
    VacancyBrief,
)
from sobesnik_web.app import create_app
from sobesnik_web.auth import COOKIE
from sobesnik_web.settings import WebSettings

SECRET = "link-secret"
CODE = "K7MQ-2XDA"
HOST = "web.test"


class FakeApi:
    """API одного инстанса: пользователи, токены, вакансия, сессия и ответы в памяти."""

    def __init__(self) -> None:
        self.tokens: dict[str, uuid.UUID] = {}
        self.telegram_user = uuid.uuid4()
        self.local_user = uuid.uuid4()
        self.links = {SECRET: self.telegram_user, CODE: self.telegram_user}
        self.llm = LlmStatus(ok=True, model="qwen3.5:4b")
        self.vacancy = Vacancy(
            id=uuid.uuid4(),
            title="Python-разработчик",
            level="middle",
            stack=["python", "postgresql"],
            requirements=[
                Requirement(
                    id=uuid.uuid4(),
                    text="Asyncio",
                    is_required=True,
                    topic="python.asyncio",
                    topic_title="Python: asyncio",
                ),
                Requirement(
                    id=uuid.uuid4(),
                    text="Индексы",
                    is_required=False,
                    topic="db.indexes",
                    topic_title="БД: индексы",
                ),
            ],
        )
        self.session = Session(
            id=uuid.uuid4(),
            vacancy_id=self.vacancy.id,
            mode="vacancy",
            status="active",
            questions=[
                SessionQuestion(
                    id=uuid.uuid4(),
                    position=i,
                    text=f"Вопрос {i}: зачем нужен индекс?",
                    topic="db.indexes",
                    docs=[{"title": "PostgreSQL: индексы", "url": "https://example.org/idx"}],
                )
                for i in (1, 2)
            ],
        )
        self.answers: dict[uuid.UUID, Answer] = {}
        self.calls: list[tuple[str, Any]] = []
        self.voice: list[tuple[bytes, str, str]] = []
        self.fail: dict[str, ApiError] = {}
        """Имя метода → ошибка, которую он бросит."""
        self.progress = Progress(topics=[], sessions=[])
        self.review = Review(
            reference="Индекс упорядочен по первому столбцу.",
            points=[
                ReviewPoint(n=1, text="упорядочен по a", status="covered", comment="верно"),
                ReviewPoint(n=2, text="нужен индекс по b", status="missed", comment="почему"),
            ],
            mistakes=[ReviewMistake(quote="индекс всегда <быстрее>", correction="не всегда")],
            question_issue="в индексе (id, status) первый — id",
            docs=[Doc(title="Индексы", url="https://example.org/idx")],
        )

    def _check(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]

    # --- Gate ---

    async def redeem(self, *, secret: str | None = None, code: str | None = None) -> Token:
        self._check("redeem", secret, code)
        user = self.links.pop(secret or (code or "").upper(), None)
        if user is None:
            raise ApiError(404, "link_invalid", "ссылка недействительна")
        return self._token(user)

    async def local_login(self) -> Token:
        self._check("local_login")
        return self._token(self.local_user)

    def _token(self, user: uuid.UUID) -> Token:
        token = f"sbu_{uuid.uuid4().hex}"
        self.tokens[token] = user
        return Token(token=token, user_id=user, expires_at=datetime.now(UTC) + timedelta(days=30))

    def for_user(self, token: str) -> "UserApi":
        return UserApi(self, token)

    async def aclose(self) -> None:
        pass


class UserApi:
    def __init__(self, fake: FakeApi, token: str) -> None:
        self.fake = fake
        self.token = token

    def _user(self) -> uuid.UUID:
        user = self.fake.tokens.get(self.token)
        if user is None:
            raise ApiError(401, "unauthorized", "сессия истекла")
        return user

    async def me(self) -> Me:
        user = self._user()
        return Me(
            user_id=user,
            telegram=user == self.fake.telegram_user,
            local=user == self.fake.local_user,
        )

    async def llm_status(self) -> LlmStatus:
        return self.fake.llm

    async def vacancies(self, user_id: uuid.UUID) -> list[VacancyBrief]:
        v = self.fake.vacancy
        return [VacancyBrief(id=v.id, title=v.title, level=v.level, created_at=datetime.now(UTC))]

    async def create_vacancy(
        self, *, text: str | None = None, url: str | None = None, html: str | None = None
    ) -> Vacancy:
        self.fake._check("create_vacancy", text, url, html)
        return self.fake.vacancy

    async def get_vacancy(self, vacancy_id: uuid.UUID) -> Vacancy:
        self.fake._check("get_vacancy", vacancy_id)
        return self.fake.vacancy

    async def set_level(self, vacancy_id: uuid.UUID, level: Level) -> Vacancy:
        self.fake._check("set_level", vacancy_id, level)
        self.fake.vacancy.level = level
        return self.fake.vacancy

    async def remove_requirement(self, vacancy_id: uuid.UUID, requirement_id: uuid.UUID) -> Vacancy:
        self.fake._check("remove_requirement", requirement_id)
        v = self.fake.vacancy
        v.requirements = [r for r in v.requirements if r.id != requirement_id]
        return v

    async def create_session(
        self,
        vacancy_id: uuid.UUID,
        n: int,
        topics: list[str] | None,
        required_only: bool = False,
        difficulty: Difficulty = "same",
    ) -> Session:
        self.fake._check("create_session", vacancy_id, n, topics, required_only, difficulty)
        return self.fake.session

    async def create_repeat(
        self, n: int, difficulty: Difficulty = "same", *, force: bool = False
    ) -> Session:
        if force:
            self.fake._check("create_repeat", n, difficulty, "force")
        else:
            self.fake._check("create_repeat", n, difficulty)
        return self.fake.session

    async def get_session(self, session_id: uuid.UUID) -> Session:
        self.fake._check("get_session", session_id)
        return self.fake.session

    async def finish_session(self, session_id: uuid.UUID) -> Summary:
        self.fake._check("finish_session", session_id)
        s = self.fake.session
        s.status = "finished"
        return Summary(
            id=s.id,
            questions=[
                SummaryQuestion(
                    position=q.position,
                    question_id=q.id,
                    answer_status=q.answer.status if q.answer else None,
                    total=q.answer.total if q.answer else None,
                    passed=q.answer.passed if q.answer else None,
                )
                for q in s.questions
            ],
            answered=sum(q.answer is not None for q in s.questions),
            evaluated=sum(q.answer is not None and q.answer.total is not None for q in s.questions),
            passed=sum(bool(q.answer and q.answer.passed) for q in s.questions),
        )

    def _new_answer(self, question_id: uuid.UUID, kind: str, status: str) -> Accepted:
        answer = Answer(
            id=uuid.uuid4(),
            session_id=self.fake.session.id,
            question_id=question_id,
            kind=kind,
            status=status,
            error=None,
            created_at=datetime.now(UTC),
        )
        self.fake.answers[answer.id] = answer
        question = next(q for q in self.fake.session.questions if q.id == question_id)
        question.answer = SessionAnswer(
            id=answer.id, kind=kind, status=status, total=None, passed=None
        )
        return Accepted(answer_id=answer.id, status=status)

    async def answer_text(
        self, session_id: uuid.UUID, question_id: uuid.UUID, text: str
    ) -> Accepted:
        self.fake._check("answer_text", question_id, text)
        return self._new_answer(question_id, "text", "evaluating")

    async def answer_voice(
        self,
        session_id: uuid.UUID,
        question_id: uuid.UUID,
        data: bytes,
        filename: str,
        content_type: str,
    ) -> Accepted:
        self.fake._check("answer_voice", question_id)
        self.fake.voice.append((data, filename, content_type))
        return self._new_answer(question_id, "voice", "received")

    async def get_answer(self, answer_id: uuid.UUID) -> Answer:
        self.fake._check("get_answer", answer_id)
        answer = self.fake.answers.get(answer_id)
        if answer is None:
            raise ApiError(404, "answer_not_found", "ответ не найден")
        return answer

    async def review_answer(self, answer_id: uuid.UUID) -> Review:
        self.fake._check("review_answer", answer_id)
        answer = self.fake.answers[answer_id]
        self.fake.answers[answer_id] = answer.model_copy(update={"has_review": True})
        return self.fake.review

    async def get_review(self, answer_id: uuid.UUID) -> Review:
        self.fake._check("get_review", answer_id)
        return self.fake.review

    async def get_progress(self, user_id: uuid.UUID) -> Progress:
        self.fake._check("get_progress", user_id)
        return self.fake.progress

    async def forget(self, user_id: uuid.UUID, scope: Literal["audio", "all"]) -> Forgotten:
        self.fake._check("forget", user_id, scope)
        if scope == "audio":
            return Forgotten(audio_deleted=2, transcripts_deleted=2)
        return Forgotten(
            audio_deleted=2,
            transcripts_deleted=2,
            answers_deleted=5,
            sessions_deleted=1,
            vacancies_deleted=1,
        )

    async def logout(self, *, everywhere: bool = False) -> None:
        self.fake._check("logout", everywhere)
        user = self._user()
        for token, owner in list(self.fake.tokens.items()):
            if token == self.token or (everywhere and owner == user):
                del self.fake.tokens[token]


@pytest.fixture
def fake() -> FakeApi:
    return FakeApi()


@pytest.fixture
async def web(fake: FakeApi) -> AsyncIterator[AsyncClient]:
    """Клиент без входа."""
    app = create_app(WebSettings(), gate=fake)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=f"http://{HOST}") as client:
            yield client


@pytest.fixture
async def browser(web: AsyncClient, fake: FakeApi) -> AsyncClient:
    """Клиент, вошедший по ссылке из бота (тот же пользователь, что в Telegram)."""
    response = await web.post(f"/login/{SECRET}")
    assert response.status_code == 303, response.text
    assert web.cookies.get(COOKIE)
    return web
