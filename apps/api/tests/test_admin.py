"""Сервисный и админ-токены, служебные эндпоинты `/admin/*` (006 §5)."""

import csv
import io
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.app import create_app
from sobesnik_api.export import COMMON
from sobesnik_api.settings import Settings
from sobesnik_api.worker import WorkerDeps, process_answer, process_run
from sobesnik_asr import MemoryStorage
from sobesnik_llm import FakeProvider

SERVICE = "service-secret"
ADMIN = "admin-secret"

PROFILE = {
    "title": "Python-разработчик",
    "level": "middle",
    "stack": ["python"],
    "requirements": [{"text": "Asyncio", "is_required": True, "topic": "python.asyncio"}],
}

META = {
    "name": "mini",
    "license": "CC-BY-4.0",
    "lang": "en",
    "score_key": "avg",
    "score_min": 0,
    "score_max": 5,
}


def jsonl(rows: list[dict[str, Any]], meta: dict[str, Any] = META) -> bytes:
    lines = [json.dumps({"dataset": meta}), *(json.dumps(r) for r in rows)]
    return "\n".join(lines).encode()


def text_row(n: int) -> dict[str, Any]:
    return {
        "external_id": f"A{n}",
        "question_id": "Q1",
        "question": "What is a stack?",
        "reference_answer": "LIFO.",
        "answer": f"answer {n}",
        "human_scores": {"avg": n % 6},
    }


@pytest.fixture
async def secured_app(
    database_url: str,
    engine: AsyncEngine,
    llm: FakeProvider,
    web: Any,
    queue: Any,
    storage: MemoryStorage,
) -> AsyncIterator[FastAPI]:
    settings = Settings(
        database_url=database_url,
        api_service_token=SecretStr(SERVICE),
        admin_token=SecretStr(ADMIN),
    )
    app = create_app(settings, llm=llm, fetcher=web.fetcher(), queue=queue, storage=storage)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def raw(secured_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Клиент без токена."""
    transport = ASGITransport(app=secured_app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_service_token(raw: AsyncClient) -> None:
    assert (await raw.get("/health")).status_code == 200  # без токена
    response = await raw.put("/users/telegram/1")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert (await raw.put("/users/telegram/1", headers=bearer("wrong"))).status_code == 401
    assert (await raw.put("/users/telegram/1", headers=bearer(ADMIN))).status_code == 401
    assert (await raw.put("/users/telegram/1", headers=bearer(SERVICE))).status_code == 201


async def test_no_service_token_means_open(client: AsyncClient) -> None:
    assert (await client.put("/users/telegram/1")).status_code == 201


async def test_admin_disabled_without_token(client: AsyncClient) -> None:
    response = await client.get("/admin/datasets")
    assert response.status_code == 404


async def test_admin_token(raw: AsyncClient) -> None:
    assert (await raw.get("/admin/datasets")).status_code == 401
    assert (await raw.get("/admin/datasets", headers=bearer(SERVICE))).status_code == 401
    response = await raw.get("/admin/datasets", headers=bearer(ADMIN))
    assert response.status_code == 200
    assert response.json() == []


async def test_dataset_import_and_list(raw: AsyncClient) -> None:
    files = {"file": ("mini.jsonl", jsonl([text_row(i) for i in range(4)]), "application/json")}
    response = await raw.post("/admin/datasets", headers=bearer(ADMIN), files=files)
    assert response.status_code == 201, response.text
    assert response.json() == {"dataset": "mini", "questions": 1, "answers": 4}

    again = await raw.post("/admin/datasets", headers=bearer(ADMIN), files=files)
    assert again.status_code == 409

    sampled = await raw.post(
        "/admin/datasets",
        headers=bearer(ADMIN),
        files=files,
        data={"sample": "2", "name": "mini-2"},
    )
    assert sampled.status_code == 201, sampled.text
    assert sampled.json()["answers"] == 2

    listed = (await raw.get("/admin/datasets", headers=bearer(ADMIN))).json()
    assert [(d["name"], d["answers"]) for d in listed] == [("mini", 4), ("mini-2", 2)]


async def test_dataset_import_errors(raw: AsyncClient) -> None:
    bad = {"file": ("x.jsonl", b"not json", "application/json")}
    response = await raw.post("/admin/datasets", headers=bearer(ADMIN), files=bad)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "bad_dataset"

    row = text_row(1)
    del row["answer"]
    row["audio"] = "a1.ogg"
    voice = {"file": ("v.jsonl", jsonl([row]), "application/json")}
    response = await raw.post("/admin/datasets", headers=bearer(ADMIN), files=voice)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "voice_dataset_cli_only"


async def test_eval_run_and_csv(
    raw: AsyncClient, deps: WorkerDeps, llm: FakeProvider, queue: Any, replies: Any
) -> None:
    files = {"file": ("mini.jsonl", jsonl([text_row(i) for i in range(2)]), "application/json")}
    await raw.post("/admin/datasets", headers=bearer(ADMIN), files=files)

    response = await raw.post(
        "/admin/eval-runs",
        headers=bearer(ADMIN),
        json={"name": "http", "dataset": "mini", "model": "fake-model", "seed": 7},
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert (run["status"], run["model"], run["params"]) == ("queued", "fake-model", {"seed": 7})
    assert queue.runs == [uuid.UUID(run["id"])]

    llm.replies = [replies.evaluation()]
    await process_run(deps, uuid.UUID(run["id"]))
    status = (await raw.get(f"/admin/eval-runs/{run['id']}", headers=bearer(ADMIN))).json()
    assert (status["status"], status["total"], status["done"]) == ("done", 2, 2)

    restarted = await raw.post(f"/admin/eval-runs/{run['id']}/restart", headers=bearer(ADMIN))
    assert restarted.status_code == 202
    assert len(queue.runs) == 2

    csv_response = await raw.get(
        "/admin/evaluations.csv", headers=bearer(ADMIN), params={"run": run["id"]}
    )
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    assert len(rows) == 2
    assert set(COMMON) <= set(rows[0])
    assert {r["dataset"] for r in rows} == {"mini"}

    long = await raw.get(
        "/admin/evaluations.csv",
        headers=bearer(ADMIN),
        params={"run": run["id"], "format": "long"},
    )
    assert len(list(csv.DictReader(io.StringIO(long.content.decode("utf-8-sig"))))) == 2 * 5


async def test_eval_run_errors(raw: AsyncClient) -> None:
    missing = await raw.post(
        "/admin/eval-runs", headers=bearer(ADMIN), json={"name": "x", "dataset": "nope"}
    )
    assert missing.status_code == 404
    assert (
        await raw.get(f"/admin/eval-runs/{uuid.uuid4()}", headers=bearer(ADMIN))
    ).status_code == 404
    no_filter = await raw.get("/admin/evaluations.csv", headers=bearer(ADMIN))
    assert no_filter.status_code == 422


async def test_live_csv(
    raw: AsyncClient, deps: WorkerDeps, llm: FakeProvider, replies: Any
) -> None:
    """«Боевые» оценки пользователя — тем же эндпоинтом с `live=true`."""
    auth = bearer(SERVICE)
    user = (await raw.put("/users/telegram/5", headers=auth)).json()
    llm.replies = [replies.parse(PROFILE)]
    vacancy = (
        await raw.post(
            "/vacancies", headers=auth, json={"user_id": user["id"], "text": replies.vacancy_text}
        )
    ).json()
    llm.replies = [replies.questions(3)]
    session = (
        await raw.post("/sessions", headers=auth, json={"vacancy_id": vacancy["id"], "n": 3})
    ).json()
    question = session["questions"][0]
    accepted = await raw.post(
        f"/sessions/{session['id']}/questions/{question['id']}/answer",
        headers=auth,
        json={"text": "ответ"},
    )
    llm.replies = [replies.evaluation()]
    await process_answer(deps, uuid.UUID(accepted.json()["answer_id"]))

    response = await raw.get(
        "/admin/evaluations.csv", headers=bearer(ADMIN), params={"live": "true"}
    )
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 1
