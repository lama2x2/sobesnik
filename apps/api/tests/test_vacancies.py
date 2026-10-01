import json
import uuid
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_core.limits import MAX_VACANCY_CHARS, MIN_VACANCY_CHARS
from sobesnik_core.prompt import load_prompt
from sobesnik_llm import FakeProvider, LLMError, LLMUnavailableError

HH_PAGE = """\
<html><head><link rel="canonical" href="https://hh.ru/vacancy/1"></head><body>
<nav>Меню сайта</nav>
<h1 data-qa="vacancy-title">Python-разработчик</h1>
<p><span data-qa="vacancy-experience">1–3 года</span></p>
<div data-qa="vacancy-description"><p>Разрабатываем платёжный сервис.</p>
<p><strong>Требования:</strong></p><ul><li>asyncio и FastAPI</li><li>PostgreSQL: индексы</li></ul>
<p>Будет плюсом: Kubernetes.</p></div>
<div><span data-qa="skills-element">Python</span><span data-qa="skills-element">SQL</span></div>
</body></html>
"""


async def post(client: AsyncClient, user: dict[str, Any], **fields: str) -> httpx.Response:
    return await client.post("/vacancies", json={"user_id": user["id"], **fields})


async def test_create_vacancy(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    engine: AsyncEngine,
    profile: dict[str, Any],
    replies: Any,
) -> None:
    profile["stack"] = ["Python", "K8s", "postgres"]
    profile["requirements"][2]["text"] = "работа с kubernetes"
    llm.replies = [replies.parse(profile)]

    response = await post(client, user, text=f"  {replies.vacancy_text}  ")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user_id"] == user["id"]
    assert body["title"] == profile["title"]
    assert body["level"] == "middle"
    assert body["stack"] == ["python", "kubernetes", "postgresql"]
    assert [(r["text"], r["is_required"], r["topic"]) for r in body["requirements"]] == [
        ("Asyncio", True, "python.asyncio"),
        ("PostgreSQL: индексы и транзакции", True, "db.indexes"),
        ("Работа с kubernetes", False, "devops.kubernetes"),
    ]
    assert (body["source"], body["source_url"]) == ("text", None)
    assert body["parser_model"] == "fake-model"
    assert body["prompt_version"] == load_prompt("parse_vacancy").version

    request = llm.requests[0]
    assert replies.vacancy_text in request.prompt
    assert "db.indexes — Индексы и планы запросов" in request.system
    assert request.json_schema is not None
    assert "topic" in request.json_schema["$defs"]["Requirement"]["required"]
    assert (request.temperature, request.seed) == (0.0, 42)

    async with engine.connect() as conn:
        raw_text = await conn.scalar(
            text("SELECT raw_text FROM vacancy WHERE id = :id"), {"id": body["id"]}
        )
        topics = (
            await conn.scalars(
                text("SELECT topic_slug FROM requirement WHERE vacancy_id = :id ORDER BY position"),
                {"id": body["id"]},
            )
        ).all()
    assert raw_text == replies.vacancy_text
    assert topics == ["python.asyncio", "db.indexes", "devops.kubernetes"]


async def test_retry_on_bad_output(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    profile: dict[str, Any],
    replies: Any,
) -> None:
    bad = json.loads(json.dumps(profile))
    bad["requirements"][0]["topic"] = "misc.python"  # неизвестная область — не чинится
    llm.replies = ["не JSON", replies.parse(bad), replies.parse(profile)]
    response = await post(client, user, text=replies.vacancy_text)
    assert response.status_code == 201, response.text
    assert len(llm.requests) == 3
    assert "темы misc.python нет в справочнике" in llm.requests[2].prompt
    assert all(r.max_tokens == 2048 for r in llm.requests)


@pytest.mark.parametrize(
    ("invented", "expected"), [("devops.helm", "devops.general"), ("python", "python.general")]
)
async def test_invented_topic_of_known_area_repaired_without_retry(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    profile: dict[str, Any],
    replies: Any,
    invented: str,
    expected: str,
) -> None:
    profile["requirements"][2]["topic"] = invented
    llm.replies = [replies.parse(profile)]
    response = await post(client, user, text=replies.vacancy_text)
    assert response.status_code == 201, response.text
    assert len(llm.requests) == 1
    assert response.json()["requirements"][2]["topic"] == expected


@pytest.mark.parametrize(
    "reply",
    [
        "не JSON",
        json.dumps({"kind": "vacancy", "profile": None}),
        json.dumps({"kind": "vacancy", "profile": {**{"title": "x"}}}),
    ],
    ids=["not-json", "no-profile", "bad-profile"],
)
async def test_bad_llm_output_after_retries(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    engine: AsyncEngine,
    reply: str,
    replies: Any,
) -> None:
    llm.replies = [reply]
    response = await post(client, user, text=replies.vacancy_text)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_bad_output"
    assert len(llm.requests) == 3
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM vacancy")) == 0


@pytest.mark.parametrize(("kind", "phrase"), [("resume", "резюме"), ("other", "Не похоже")])
async def test_not_a_vacancy(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    engine: AsyncEngine,
    kind: str,
    phrase: str,
    replies: Any,
) -> None:
    llm.replies = [replies.parse(kind=kind)]
    response = await post(client, user, text=replies.vacancy_text)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "not_a_vacancy"
    assert phrase in error["message"]
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM vacancy")) == 0


@pytest.mark.parametrize("short", ["вакансия", "python junior", "x" * (MIN_VACANCY_CHARS - 1)])
async def test_short_text_rejected_without_llm(
    client: AsyncClient, llm: FakeProvider, user: dict[str, Any], short: str
) -> None:
    response = await post(client, user, text=short)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "not_a_vacancy"
    assert llm.requests == []


@pytest.mark.parametrize(
    "fields",
    [
        {"text": ""},
        {"text": "   "},
        {"text": "x" * (MAX_VACANCY_CHARS + 1)},
        {},
        {"text": "x" * 100, "url": "https://hh.ru/vacancy/1"},
        {"url": "https://hh.ru/vacancy/1", "html": "<p>x</p>"},
    ],
    ids=["empty", "spaces", "too-long", "none", "text+url", "url+html"],
)
async def test_invalid_request(
    client: AsyncClient, llm: FakeProvider, user: dict[str, Any], fields: dict[str, str]
) -> None:
    response = await post(client, user, **fields)
    assert response.status_code == 422
    assert "error" not in response.json()
    assert llm.requests == []


async def test_max_length_text_accepted(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    profile: dict[str, Any],
    replies: Any,
) -> None:
    llm.replies = [replies.parse(profile)]
    response = await post(client, user, text="x" * MAX_VACANCY_CHARS)
    assert response.status_code == 201


async def test_unknown_user(client: AsyncClient, llm: FakeProvider, replies: Any) -> None:
    response = await client.post(
        "/vacancies", json={"user_id": str(uuid.uuid4()), "text": replies.vacancy_text}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"
    assert llm.requests == []


async def test_llm_unavailable(
    client: AsyncClient, llm: FakeProvider, user: dict[str, Any], replies: Any
) -> None:
    llm.replies = [LLMUnavailableError("нет соединения")]
    response = await post(client, user, text=replies.vacancy_text)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"
    assert len(llm.requests) == 1


async def test_llm_error(
    client: AsyncClient, llm: FakeProvider, user: dict[str, Any], replies: Any
) -> None:
    llm.replies = [LLMError("400")]
    response = await post(client, user, text=replies.vacancy_text)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_error"


async def test_create_from_hh_url(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    profile: dict[str, Any],
    web: Any,
    engine: AsyncEngine,
    replies: Any,
) -> None:
    url = "https://hh.ru/vacancy/1?from=share"
    web.pages[url] = replies.html(HH_PAGE)
    llm.replies = [replies.parse(profile)]

    response = await post(client, user, url=url)

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["source"], body["source_url"]) == ("url", url)
    prompt = llm.requests[0].prompt
    assert "Должность: Python-разработчик" in prompt
    assert "Требуемый опыт работы: 1–3 года" in prompt
    assert "- PostgreSQL: индексы" in prompt
    assert "Ключевые навыки: Python, SQL" in prompt
    assert "Меню сайта" not in prompt
    assert "Sobesnik" in web.requests[0].headers["user-agent"]


async def test_create_from_html(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    profile: dict[str, Any],
    replies: Any,
) -> None:
    llm.replies = [replies.parse(profile)]
    response = await post(client, user, html=HH_PAGE)
    assert response.status_code == 201, response.text
    assert response.json()["source"] == "html"
    assert "Ключевые навыки: Python, SQL" in llm.requests[0].prompt


@pytest.mark.parametrize(
    ("page", "code"),
    [
        (httpx.Response(404), "url_unreadable"),
        (httpx.Response(200, json={"a": 1}), "url_unreadable"),
        (httpx.ConnectError("refused"), "url_unreadable"),
        ("<html><body><p>Капча</p></body></html>", "url_unreadable"),
    ],
    ids=["404", "json", "connect", "no-text"],
)
async def test_url_unreadable(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    web: Any,
    page: object,
    code: str,
    replies: Any,
) -> None:
    url = "https://example.com/job"
    web.pages[url] = replies.html(page) if isinstance(page, str) else page
    response = await post(client, user, url=url)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == code
    assert "Пришлите текст вакансии или сохранённую страницу (.html)" in error["message"]
    assert llm.requests == []


@pytest.mark.parametrize(
    "url", ["ftp://hh.ru/vacancy/1", "http://localhost:8000/", "http://10.0.0.1/job"]
)
async def test_url_not_allowed(
    client: AsyncClient, llm: FakeProvider, user: dict[str, Any], web: Any, url: str
) -> None:
    web.dns["localhost"] = ["127.0.0.1"]
    web.dns["10.0.0.1"] = ["10.0.0.1"]
    response = await post(client, user, url=url)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "url_not_allowed"
    assert web.requests == []
    assert llm.requests == []


async def test_html_unreadable(
    client: AsyncClient, llm: FakeProvider, user: dict[str, Any]
) -> None:
    response = await post(client, user, html="<html><body><p>пусто</p></body></html>")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "html_unreadable"
    assert llm.requests == []


async def test_get_vacancy(client: AsyncClient, vacancy: dict[str, Any]) -> None:
    response = await client.get(f"/vacancies/{vacancy['id']}")
    assert response.status_code == 200
    assert response.json() == vacancy


async def test_get_unknown_vacancy(client: AsyncClient) -> None:
    response = await client.get(f"/vacancies/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "vacancy_not_found"


async def test_patch_level_and_remove_requirement(
    client: AsyncClient, vacancy: dict[str, Any]
) -> None:
    removed = vacancy["requirements"][0]["id"]
    response = await client.patch(
        f"/vacancies/{vacancy['id']}", json={"level": "senior", "remove_requirements": [removed]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["level"] == "senior"
    assert [r["id"] for r in body["requirements"]] == [r["id"] for r in vacancy["requirements"][1:]]
    assert (await client.get(f"/vacancies/{vacancy['id']}")).json() == body


async def test_patch_keeps_questions_of_removed_requirement(
    client: AsyncClient,
    llm: FakeProvider,
    vacancy: dict[str, Any],
    engine: AsyncEngine,
    replies: Any,
) -> None:
    llm.replies = [replies.questions(3)]
    session = (await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})).json()
    removed = vacancy["requirements"][0]["id"]
    response = await client.patch(
        f"/vacancies/{vacancy['id']}", json={"remove_requirements": [removed]}
    )
    assert response.status_code == 200
    questions = (await client.get(f"/sessions/{session['id']}")).json()["questions"]
    assert questions[0]["requirement_id"] is None
    assert questions[0]["topic"] == "python.asyncio"


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({}, 422, None),
        ({"level": "staff"}, 422, None),
        ({"remove_requirements": []}, 422, None),
        (
            {"remove_requirements": ["00000000-0000-0000-0000-000000000000"]},
            422,
            "requirement_not_found",
        ),
        ("all", 422, "no_requirements_left"),
    ],
)
async def test_patch_errors(
    client: AsyncClient, vacancy: dict[str, Any], body: Any, status: int, code: str | None
) -> None:
    if body == "all":
        body = {"remove_requirements": [r["id"] for r in vacancy["requirements"]]}
    response = await client.patch(f"/vacancies/{vacancy['id']}", json=body)
    assert response.status_code == status
    if code:
        assert response.json()["error"]["code"] == code


async def test_patch_unknown_vacancy(client: AsyncClient) -> None:
    response = await client.patch(f"/vacancies/{uuid.uuid4()}", json={"level": "junior"})
    assert response.status_code == 404
