import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_core.limits import MAX_VACANCY_CHARS
from sobesnik_llm import FakeProvider, LLMError, LLMUnavailableError


async def test_create_vacancy(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    engine: AsyncEngine,
    profile: dict[str, Any],
) -> None:
    llm.replies = [json.dumps(profile, ensure_ascii=False)]

    response = await client.post(
        "/vacancies", json={"user_id": user["id"], "text": "  Ищем Python-разработчика  "}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user_id"] == user["id"]
    assert body["title"] == profile["title"]
    assert body["level"] == "middle"
    assert body["stack"] == profile["stack"]
    assert [(r["text"], r["is_required"]) for r in body["requirements"]] == [
        (r["text"], r["is_required"]) for r in profile["requirements"]
    ]
    assert body["parser_model"] == "fake-model"
    assert body["prompt_version"] == "v0"

    request = llm.requests[0]
    assert "Ищем Python-разработчика" in request.prompt
    assert request.json_schema is not None

    async with engine.connect() as conn:
        raw_text = await conn.scalar(
            text("SELECT raw_text FROM vacancy WHERE id = :id"), {"id": body["id"]}
        )
        count = await conn.scalar(
            text("SELECT count(*) FROM requirement WHERE vacancy_id = :id"), {"id": body["id"]}
        )
    assert raw_text == "Ищем Python-разработчика"
    assert count == 3


@pytest.mark.parametrize("vacancy_text", ["", "   ", "x" * (MAX_VACANCY_CHARS + 1)])
async def test_invalid_text(
    client: AsyncClient, llm: FakeProvider, user: dict[str, Any], vacancy_text: str
) -> None:
    response = await client.post("/vacancies", json={"user_id": user["id"], "text": vacancy_text})
    assert response.status_code == 422
    assert llm.requests == []


async def test_max_length_text_accepted(
    client: AsyncClient, llm: FakeProvider, user: dict[str, Any], profile: dict[str, Any]
) -> None:
    llm.replies = [json.dumps(profile)]
    response = await client.post(
        "/vacancies", json={"user_id": user["id"], "text": "x" * MAX_VACANCY_CHARS}
    )
    assert response.status_code == 201


async def test_unknown_user(client: AsyncClient, llm: FakeProvider) -> None:
    response = await client.post("/vacancies", json={"user_id": str(uuid.uuid4()), "text": "v"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"
    assert llm.requests == []


@pytest.mark.parametrize(
    "patch",
    [None, {"level": "staff"}, {"requirements": []}],
    ids=["not-json", "bad-level", "no-requirements"],
)
async def test_bad_llm_output(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    engine: AsyncEngine,
    profile: dict[str, Any],
    patch: dict[str, Any] | None,
) -> None:
    llm.replies = ["не JSON" if patch is None else json.dumps({**profile, **patch})]
    response = await client.post("/vacancies", json={"user_id": user["id"], "text": "v"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_bad_output"
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM vacancy")) == 0


async def test_llm_unavailable(
    client: AsyncClient, llm: FakeProvider, user: dict[str, Any]
) -> None:
    llm.replies = [LLMUnavailableError("нет соединения")]
    response = await client.post("/vacancies", json={"user_id": user["id"], "text": "v"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"


async def test_llm_error(client: AsyncClient, llm: FakeProvider, user: dict[str, Any]) -> None:
    llm.replies = [LLMError("400")]
    response = await client.post("/vacancies", json={"user_id": user["id"], "text": "v"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_error"
