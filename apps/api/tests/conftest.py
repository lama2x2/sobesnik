"""Тесты API ходят в настоящий Postgres (БД из TEST_DATABASE_URL) и в FakeProvider вместо LLM."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncpg
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
from sobesnik_api.settings import Settings
from sobesnik_llm import FakeProvider

API_DIR = Path(__file__).resolve().parents[1]

PROFILE = {
    "title": "Python-разработчик",
    "level": "middle",
    "stack": ["python", "fastapi", "postgresql"],
    "requirements": [
        {"text": "asyncio", "is_required": True},
        {"text": "PostgreSQL: индексы и транзакции", "is_required": True},
        {"text": "Kubernetes", "is_required": False},
    ],
}


def questions_reply(n: int, requirement_index: int | None = 0) -> str:
    return json.dumps(
        {
            "questions": [
                {"text": f"Вопрос {i + 1}", "requirement_index": requirement_index}
                for i in range(n)
            ]
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


QuestionsReply = Callable[..., str]
RunAlembic = Callable[[str, str], Awaitable[None]]


@pytest.fixture
def profile() -> dict[str, Any]:
    """Профиль, который FakeProvider отдаёт при разборе вакансии."""
    data: dict[str, Any] = json.loads(json.dumps(PROFILE))
    return data


@pytest.fixture
def make_questions_reply() -> QuestionsReply:
    return questions_reply


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


@pytest.fixture
async def app(database_url: str, engine: AsyncEngine, llm: FakeProvider) -> AsyncIterator[FastAPI]:
    app = create_app(Settings(database_url=database_url), llm=llm)
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
    llm.replies = [json.dumps(PROFILE, ensure_ascii=False)]
    response = await client.post("/vacancies", json={"user_id": user["id"], "text": "Вакансия"})
    assert response.status_code == 201, response.text
    llm.replies = []
    llm.requests.clear()
    data: dict[str, Any] = response.json()
    return data
