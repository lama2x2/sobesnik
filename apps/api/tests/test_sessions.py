import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_core.limits import QUESTIONS_MAX, QUESTIONS_MIN
from sobesnik_llm import FakeProvider, LLMUnavailableError

QuestionsReply = Callable[..., str]


async def test_create_session_default_n(
    client: AsyncClient,
    llm: FakeProvider,
    vacancy: dict[str, Any],
    engine: AsyncEngine,
    make_questions_reply: QuestionsReply,
) -> None:
    llm.replies = [make_questions_reply(5, requirement_index=1)]

    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"]})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["vacancy_id"] == vacancy["id"]
    assert body["mode"] == "vacancy"
    assert body["status"] == "active"
    assert [q["position"] for q in body["questions"]] == [1, 2, 3, 4, 5]
    assert [q["text"] for q in body["questions"]] == [f"Вопрос {i}" for i in range(1, 6)]
    second_requirement = vacancy["requirements"][1]["id"]
    assert {q["requirement_id"] for q in body["questions"]} == {second_requirement}

    request = llm.requests[0]
    assert "Составь ровно 5 вопросов" in request.prompt
    assert "PostgreSQL: индексы и транзакции" in request.prompt
    assert request.json_schema is not None
    assert request.json_schema["properties"]["questions"]["minItems"] == 5

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT q.gen_model, q.prompt_version, q.source FROM session_question sq "
                    "JOIN question q ON q.id = sq.question_id WHERE sq.session_id = :id"
                ),
                {"id": body["id"]},
            )
        ).all()
        user_id = await conn.scalar(
            text("SELECT user_id FROM session WHERE id = :id"), {"id": body["id"]}
        )
    assert rows == [("fake-model", "v0", "generated")] * 5
    assert str(user_id) == vacancy["user_id"]


async def test_extra_questions_are_cut(
    client: AsyncClient,
    llm: FakeProvider,
    vacancy: dict[str, Any],
    make_questions_reply: QuestionsReply,
) -> None:
    llm.replies = [make_questions_reply(5)]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert response.status_code == 201
    assert len(response.json()["questions"]) == 3


@pytest.mark.parametrize("index", [None, -1, 3, 100])
async def test_unknown_requirement_index_gives_null(
    client: AsyncClient,
    llm: FakeProvider,
    vacancy: dict[str, Any],
    index: int | None,
    make_questions_reply: QuestionsReply,
) -> None:
    llm.replies = [make_questions_reply(3, requirement_index=index)]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert response.status_code == 201
    assert {q["requirement_id"] for q in response.json()["questions"]} == {None}


@pytest.mark.parametrize("n", [QUESTIONS_MIN - 1, QUESTIONS_MAX + 1])
async def test_n_out_of_range(
    client: AsyncClient, llm: FakeProvider, vacancy: dict[str, Any], n: int
) -> None:
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": n})
    assert response.status_code == 422
    assert llm.requests == []


async def test_unknown_vacancy(client: AsyncClient, llm: FakeProvider) -> None:
    response = await client.post("/sessions", json={"vacancy_id": str(uuid.uuid4())})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "vacancy_not_found"
    assert llm.requests == []


@pytest.mark.parametrize(
    "reply",
    [
        "{",
        json.dumps({"questions": [{"text": ""}]}),
        "too-few",
    ],
    ids=["not-json", "empty-text", "too-few"],
)
async def test_bad_llm_output(
    client: AsyncClient,
    llm: FakeProvider,
    vacancy: dict[str, Any],
    reply: str,
    engine: AsyncEngine,
    make_questions_reply: QuestionsReply,
) -> None:
    llm.replies = [make_questions_reply(4) if reply == "too-few" else reply]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 5})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_bad_output"
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM session")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM question")) == 0


async def test_llm_unavailable(
    client: AsyncClient, llm: FakeProvider, vacancy: dict[str, Any]
) -> None:
    llm.replies = [LLMUnavailableError("таймаут")]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"
