"""Проверка ключевых пунктов эталона в фоне (011 §6)."""

import json
import logging
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.worker import WorkerDeps, process_key_points_check
from sobesnik_llm import FakeProvider, LLMUnavailableError

from .conftest import FakeQueue, questions_reply
from .test_review import evaluated, review_reply


def check_reply(*issues: tuple[int, int, str]) -> str:
    return json.dumps(
        {"issues": [{"q": q, "n": n, "issue": issue} for q, n, issue in issues]},
        ensure_ascii=False,
    )


async def stored(engine: AsyncEngine, session: dict[str, Any]) -> list[Any]:
    ids = [q["id"] for q in session["questions"]]
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, key_points_check FROM question WHERE id = ANY(:ids)"),
            {"ids": [uuid.UUID(i) for i in ids]},
        )
        by_id = {str(r.id): r.key_points_check for r in rows}
    return [by_id[i] for i in ids]


async def test_check_is_off_by_default(
    client: AsyncClient, llm: FakeProvider, vacancy: dict[str, Any], queue: FakeQueue
) -> None:
    llm.replies = [questions_reply(3)]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert response.status_code == 201
    assert queue.key_points == []


async def test_session_queues_check_when_enabled(
    app: FastAPI,
    client: AsyncClient,
    llm: FakeProvider,
    vacancy: dict[str, Any],
    queue: FakeQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app.state.settings.key_points_check = True
    llm.replies = [questions_reply(3)]
    response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert queue.key_points == [uuid.UUID(response.json()["id"])]

    queue.broken = True  # очередь недоступна — сессия всё равно создаётся
    llm.replies = [questions_reply(3, prefix="Другой")]
    with caplog.at_level(logging.WARNING):
        response = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "n": 3})
    assert response.status_code == 201
    assert "key points check not queued" in caplog.text


async def test_check_stores_doubts_per_question(
    app: FastAPI,
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm.replies = [
        check_reply(
            (1, 2, "Неверно: на самом деле наоборот."),
            (1, 2, "повтор"),  # тот же пункт ещё раз — выбрасывается
            (2, 9, "пункта 9 нет"),
            (7, 1, "вопроса 7 нет"),
            (3, 1, "-"),  # пустое объяснение
        )
    ]
    with caplog.at_level(logging.INFO):
        assert await process_key_points_check(deps, uuid.UUID(session["id"])) == 1
    first, second, third = await stored(engine, session)
    assert first["issues"] == [{"n": 2, "issue": "Неверно: на самом деле наоборот."}]
    assert second["issues"] == third["issues"] == []
    assert first["prompt_version"].startswith("v1#")
    prompt = llm.requests[-1].prompt
    assert all(q["text"] in prompt for q in session["questions"])
    assert "наоборот" not in caplog.text  # тексты сомнений в лог не пишутся

    # повтор задачи: всё уже проверено — модель не вызывается
    calls = len(llm.requests)
    assert await process_key_points_check(deps, uuid.UUID(session["id"])) is None
    assert len(llm.requests) == calls

    # сомнение видно в разборе, только если проверка включена
    answer_id = await evaluated(client, session, deps, llm, replies)
    llm.replies = [review_reply()]
    body = (await client.post(f"/answers/{answer_id}/review")).json()
    assert [p["doubt"] for p in body["points"]] == [None, None, None]
    app.state.settings.key_points_check = True
    body = (await client.get(f"/answers/{answer_id}/review")).json()
    assert [p["doubt"] for p in body["points"]] == [None, "Неверно: на самом деле наоборот.", None]


async def test_check_failure_leaves_questions_unchecked(
    session: dict[str, Any], deps: WorkerDeps, llm: FakeProvider, engine: AsyncEngine
) -> None:
    llm.replies = [LLMUnavailableError("нет связи")]
    assert await process_key_points_check(deps, uuid.UUID(session["id"])) is None
    assert await stored(engine, session) == [None, None, None]
    assert await process_key_points_check(deps, uuid.uuid4()) is None  # сессии нет
