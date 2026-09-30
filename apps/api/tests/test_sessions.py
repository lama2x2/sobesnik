import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_core.limits import QUESTIONS_MAX, QUESTIONS_MIN
from sobesnik_core.prompt import load_prompt
from sobesnik_llm import FakeProvider, LLMUnavailableError


async def test_create_session_default_n(
    client: AsyncClient,
    llm: FakeProvider,
    vacancy: dict[str, Any],
    engine: AsyncEngine,
    replies: Any,
) -> None:
    llm.replies = [replies.questions(5)]

    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"]})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["vacancy_id"] == vacancy["id"]
    assert (body["mode"], body["status"]) == ("vacancy", "active")
    questions = body["questions"]
    assert [q["position"] for q in questions] == [1, 2, 3, 4, 5]
    assert questions[0]["text"] == "Вопрос 1: почему так устроено?"
    assert questions[0]["key_points"] == ["пункт 1.1", "пункт 1.2", "пункт 1.3"]

    # план: обязательные (0, 1), потом желательное (2), дальше по кругу
    reqs = vacancy["requirements"]
    expected = [reqs[0], reqs[1], reqs[2], reqs[0], reqs[1]]
    assert [q["requirement_id"] for q in questions] == [r["id"] for r in expected]
    assert [q["topic"] for q in questions] == [r["topic"] for r in expected]

    request = llm.requests[0]
    assert "План — 5 вопросов" in request.prompt
    assert "1. [обязательно] Asyncio (тема: python.asyncio)" in request.prompt
    assert "4. [обязательно, аспект 2] Asyncio" in request.prompt
    assert "уже задавались" not in request.prompt
    assert request.json_schema is not None
    assert request.json_schema["properties"]["questions"]["minItems"] == 5
    assert request.temperature == 0.7

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT q.gen_model, q.prompt_version, q.source, q.key_points "
                    "FROM session_question sq JOIN question q ON q.id = sq.question_id "
                    "WHERE sq.session_id = :id ORDER BY sq.position"
                ),
                {"id": body["id"]},
            )
        ).all()
        user_id = await conn.scalar(
            text("SELECT user_id FROM session WHERE id = :id"), {"id": body["id"]}
        )
    version = load_prompt("generate_questions").version
    assert [r[:3] for r in rows] == [("fake-model", version, "generated")] * 5
    assert rows[0][3] == ["пункт 1.1", "пункт 1.2", "пункт 1.3"]
    assert str(user_id) == vacancy["user_id"]


async def test_get_session(
    client: AsyncClient, llm: FakeProvider, vacancy: dict[str, Any], replies: Any
) -> None:
    llm.replies = [replies.questions(3)]
    created = (await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})).json()
    response = await client.get(f"/sessions/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


async def test_get_unknown_session(client: AsyncClient) -> None:
    response = await client.get(f"/sessions/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"


async def test_extra_questions_are_cut(
    client: AsyncClient, llm: FakeProvider, vacancy: dict[str, Any], replies: Any
) -> None:
    llm.replies = [replies.questions(5)]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert response.status_code == 201
    assert len(response.json()["questions"]) == 3


async def test_experience_question_is_retried(
    client: AsyncClient, llm: FakeProvider, vacancy: dict[str, Any], replies: Any
) -> None:
    bad = json.loads(replies.questions(3))
    bad["questions"][1]["text"] = "Приходилось ли вам настраивать индексы?"
    llm.replies = [json.dumps(bad, ensure_ascii=False), replies.questions(3)]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert response.status_code == 201, response.text
    assert len(llm.requests) == 2
    assert "questions.1: вопрос о личном опыте" in llm.requests[1].prompt


async def test_second_session_avoids_repeats(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    profile: dict[str, Any],
    replies: Any,
) -> None:
    profile["requirements"].append(
        {"text": "Кэширование в Redis", "is_required": False, "topic": "arch.caching"}
    )
    llm.replies = [replies.parse(profile)]
    vacancy = (
        await client.post("/vacancies", json={"user_id": user["id"], "text": replies.vacancy_text})
    ).json()
    reqs = vacancy["requirements"]

    llm.replies = [replies.questions(3, prefix="Первый")]
    first = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert first.status_code == 201
    assert [q["requirement_id"] for q in first.json()["questions"]] == [
        reqs[0]["id"],
        reqs[1]["id"],
        reqs[2]["id"],
    ]
    llm.requests.clear()

    # вторая сессия повторяет вопросы первой → повтор с просьбой не повторяться
    llm.replies = [replies.questions(3, prefix="Первый"), replies.questions(3, prefix="Второй")]
    second = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert second.status_code == 201, second.text

    prompt = llm.requests[0].prompt
    assert "уже задавались" in prompt
    assert "- Первый 1: почему так устроено?" in prompt
    # неспрошенное требование 3 идёт первым, дальше обязательные
    assert "1. [желательно] Кэширование в Redis" in prompt
    assert "уже задавался по вакансии" in llm.requests[1].prompt
    assert [q["requirement_id"] for q in second.json()["questions"]] == [
        reqs[3]["id"],
        reqs[0]["id"],
        reqs[1]["id"],
    ]


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
    ["{", json.dumps({"questions": [{"text": "", "key_points": ["a", "b", "c"]}]}), "too-few"],
    ids=["not-json", "empty-text", "too-few"],
)
async def test_bad_llm_output(
    client: AsyncClient,
    llm: FakeProvider,
    vacancy: dict[str, Any],
    reply: str,
    engine: AsyncEngine,
    replies: Any,
) -> None:
    llm.replies = [replies.questions(4) if reply == "too-few" else reply]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 5})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_bad_output"
    assert len(llm.requests) == 3
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
