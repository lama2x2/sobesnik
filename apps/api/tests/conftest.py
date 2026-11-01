"""Тесты API ходят в настоящий Postgres (БД из TEST_DATABASE_URL) и в FakeProvider вместо LLM."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sobesnik_api.app import create_app
from sobesnik_api.db.base import Base
from sobesnik_api.db.models import EvalRun
from sobesnik_api.db.session import make_sessionmaker
from sobesnik_api.settings import Settings
from sobesnik_api.sources.fetch import PageFetcher
from sobesnik_api.worker import WorkerDeps
from sobesnik_asr import FakeTranscriber, MemoryStorage
from sobesnik_core.rubric import load_rubric
from sobesnik_llm import FakeProvider, LLMProvider

API_DIR = Path(__file__).resolve().parents[1]

PROFILE = {
    "title": "Python-разработчик",
    "level": "middle",
    "stack": ["python", "fastapi", "postgresql"],
    "requirements": [
        {"text": "Asyncio", "is_required": True, "topic": "python.asyncio"},
        {"text": "PostgreSQL: индексы и транзакции", "is_required": True, "topic": "db.indexes"},
        {"text": "Kubernetes", "is_required": False, "topic": "devops.kubernetes"},
    ],
}

VACANCY_TEXT = (
    "Ищем Python-разработчика уровня middle. Требования: asyncio, PostgreSQL (индексы, "
    "транзакции), FastAPI. Будет плюсом: Kubernetes."
)


def parse_reply(profile: dict[str, Any] | None = None, kind: str = "vacancy") -> str:
    """Ответ модели при разборе: вид текста и профиль."""
    return json.dumps(
        {"kind": kind, "profile": profile if kind == "vacancy" else None}, ensure_ascii=False
    )


def questions_reply(n: int, prefix: str = "Вопрос") -> str:
    return json.dumps(
        {
            "questions": [
                {
                    "text": f"{prefix} {i + 1}: почему так устроено?",
                    "key_points": [f"пункт {i + 1}.{k}" for k in range(1, 4)],
                }
                for i in range(n)
            ]
        },
        ensure_ascii=False,
    )


CRITERIA = load_rubric().ids


def eval_reply(
    scores: list[int] | None = None,
    *,
    missed: list[int] | None = None,
    wrong: list[int] | None = None,
    advice: str = "Повторите тему.",
) -> str:
    """Ответ модели при оценке: баллы по критериям в порядке рубрики."""
    values = scores or [2] * len(CRITERIA)
    return json.dumps(
        {
            "scores": {
                cid: {"reason": f"обоснование {cid}", "score": value}
                for cid, value in zip(CRITERIA, values, strict=True)
            },
            "missed_points": missed or [],
            "wrong_points": wrong or [],
            "advice": advice,
        },
        ensure_ascii=False,
    )


def get_test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.exit("TEST_DATABASE_URL не задан: запускайте тесты через compose (сервис tools)")
    return url


async def recreate_database(url: str) -> None:
    parsed = make_url(url)
    conn = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await conn.close()


def alembic_config(url: str) -> Config:
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    return config


async def alembic(url: str, action: str, revision: str) -> None:
    """env.py сам крутит asyncio.run, поэтому миграции запускаются в отдельном потоке."""
    fn = command.upgrade if action == "upgrade" else command.downgrade
    await asyncio.to_thread(fn, alembic_config(url), revision)


RunAlembic = Callable[[str, str], Awaitable[None]]


@pytest.fixture
def profile() -> dict[str, Any]:
    """Профиль, который FakeProvider отдаёт при разборе вакансии."""
    data: dict[str, Any] = json.loads(json.dumps(PROFILE))
    return data


@pytest.fixture
def run_alembic(database_url: str) -> RunAlembic:
    async def run(action: str, revision: str) -> None:
        await alembic(database_url, action, revision)

    return run


@pytest.fixture(scope="session")
async def database_url() -> str:
    url = get_test_database_url()
    await recreate_database(url)
    await alembic(url, "upgrade", "head")
    return url


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_tables(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    yield
    if "engine" not in request.fixturenames:
        return
    engine: AsyncEngine = request.getfixturevalue("engine")
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def llm() -> FakeProvider:
    return FakeProvider(model="fake-model")


class FakeWeb:
    """Интернет для тестов: ответы по URL и DNS по хосту. Незнакомый хост — 93.184.216.34."""

    def __init__(self) -> None:
        self.pages: dict[str, httpx.Response | Exception] = {}
        self.dns: dict[str, list[str]] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        page = self.pages.get(str(request.url))
        if page is None:
            return httpx.Response(404)
        if isinstance(page, Exception):
            raise page
        return page

    async def resolve(self, host: str, port: int) -> list[str]:
        return self.dns.get(host, ["93.184.216.34"])

    def fetcher(self, *, allow_private: bool = False, timeout_s: float = 5.0) -> PageFetcher:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return PageFetcher(
            timeout_s=timeout_s, allow_private=allow_private, client=client, resolver=self.resolve
        )


def html_page(body: str, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        content=body.encode(),
        headers={"content-type": "text/html; charset=utf-8", **headers},
    )


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


class Replies:
    """Готовые ответы модели и страницы для тестов."""

    vacancy_text = VACANCY_TEXT
    parse = staticmethod(parse_reply)
    questions = staticmethod(questions_reply)
    evaluation = staticmethod(eval_reply)
    html = staticmethod(html_page)


class FakeQueue:
    """Очередь без Redis: запоминает поставленные задачи."""

    def __init__(self) -> None:
        self.evaluations: list[uuid.UUID] = []
        self.transcriptions: list[uuid.UUID] = []
        self.runs: list[uuid.UUID] = []
        self.broken = False

    async def enqueue_evaluation(self, answer_id: uuid.UUID) -> None:
        if self.broken:
            raise ConnectionError("redis недоступен")
        self.evaluations.append(answer_id)

    async def enqueue_transcription(self, answer_id: uuid.UUID) -> None:
        if self.broken:
            raise ConnectionError("redis недоступен")
        self.transcriptions.append(answer_id)

    async def enqueue_run(self, run_id: uuid.UUID) -> None:
        if self.broken:
            raise ConnectionError("redis недоступен")
        self.runs.append(run_id)

    async def aclose(self) -> None:
        pass


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


class RunModels:
    """Провайдеры прогонов: на каждую модель свой FakeProvider с теми же ответами, что у `llm`."""

    def __init__(self, base: FakeProvider) -> None:
        self.base = base
        self.opened: list[str] = []
        self.providers: dict[str, FakeProvider] = {}

    def provider(self, model: str) -> FakeProvider:
        if model not in self.providers:
            self.providers[model] = FakeProvider(model=model)
        provider = self.providers[model]
        provider.replies = list(self.base.replies)
        provider.available = self.base.available
        return provider

    @asynccontextmanager
    async def __call__(self, run: EvalRun) -> AsyncIterator[LLMProvider]:
        self.opened.append(run.model)
        yield self.base if run.model == self.base.model else self.provider(run.model)


@pytest.fixture
def run_models(llm: FakeProvider) -> RunModels:
    return RunModels(llm)


@pytest.fixture
def storage() -> MemoryStorage:
    return MemoryStorage()


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber("Составной индекс упорядочен по первому столбцу.", model="small")


@pytest.fixture
def deps(
    engine: AsyncEngine,
    llm: FakeProvider,
    run_models: RunModels,
    transcriber: FakeTranscriber,
    storage: MemoryStorage,
    queue: FakeQueue,
) -> WorkerDeps:
    return WorkerDeps(
        settings=Settings(eval_max_tries=3, asr_max_tries=3),
        sessionmaker=make_sessionmaker(engine),
        llm=llm,
        run_llm=run_models,
        transcriber=transcriber,
        storage=storage,
        queue=queue,
    )


@pytest.fixture
def replies() -> type[Replies]:
    return Replies


@pytest.fixture
async def app(
    database_url: str,
    engine: AsyncEngine,
    llm: FakeProvider,
    web: FakeWeb,
    queue: FakeQueue,
    storage: MemoryStorage,
) -> AsyncIterator[FastAPI]:
    app = create_app(
        Settings(database_url=database_url),
        llm=llm,
        fetcher=web.fetcher(),
        queue=queue,
        storage=storage,
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as client:
        yield client


@pytest.fixture
async def user(client: AsyncClient) -> dict[str, Any]:
    response = await client.put("/users/telegram/100")
    assert response.status_code == 201
    data: dict[str, Any] = response.json()
    return data


@pytest.fixture
async def vacancy(client: AsyncClient, llm: FakeProvider, user: dict[str, Any]) -> dict[str, Any]:
    llm.replies = [parse_reply(PROFILE)]
    response = await client.post("/vacancies", json={"user_id": user["id"], "text": VACANCY_TEXT})
    assert response.status_code == 201, response.text
    llm.replies = []
    llm.requests.clear()
    data: dict[str, Any] = response.json()
    return data


@pytest.fixture
async def session(
    client: AsyncClient, llm: FakeProvider, vacancy: dict[str, Any]
) -> dict[str, Any]:
    """Сессия из 3 вопросов, у каждого 3 ключевых пункта."""
    llm.replies = [questions_reply(3)]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert response.status_code == 201, response.text
    llm.replies = []
    llm.requests.clear()
    data: dict[str, Any] = response.json()
    return data
