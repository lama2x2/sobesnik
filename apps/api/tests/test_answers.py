"""Приём ответа, оценка воркером, GET /answers и сессия с ответами (003 §8, §10)."""

import logging
import uuid
from typing import Any

import pytest
from arq import Retry
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.worker import WorkerDeps, process_answer, recover
from sobesnik_core.limits import ANSWER_MAX_CHARS
from sobesnik_core.prompt import load_prompt
from sobesnik_core.rubric import load_rubric
from sobesnik_llm import FakeProvider, LLMError

ANSWER = "Индекс упорядочен по первому столбцу, поэтому по второму искать приходится везде."


def answer_url(session: dict[str, Any], position: int = 1) -> str:
    qid = session["questions"][position - 1]["id"]
    return f"/sessions/{session['id']}/questions/{qid}/answer"


async def submit(client: AsyncClient, session: dict[str, Any], position: int = 1) -> str:
    response = await client.post(answer_url(session, position), json={"text": ANSWER})
    assert response.status_code == 202, response.text
    answer_id: str = response.json()["answer_id"]
    return answer_id


async def test_submit_answer(
    client: AsyncClient, session: dict[str, Any], queue: Any, engine: AsyncEngine
) -> None:
    response = await client.post(answer_url(session), json={"text": f"  {ANSWER}  "})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "received"
    assert queue.evaluations == [uuid.UUID(body["answer_id"])]
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT text, status, kind, source, user_id FROM answer WHERE id = :id"),
                {"id": body["answer_id"]},
            )
        ).one()
    assert row.text == ANSWER
    assert (row.status, row.kind, row.source) == ("received", "text", "user")
    assert row.user_id is not None


@pytest.mark.parametrize("value", ["", "   ", "x" * (ANSWER_MAX_CHARS + 1)])
async def test_answer_length(client: AsyncClient, session: dict[str, Any], value: str) -> None:
    response = await client.post(answer_url(session), json={"text": value})
    assert response.status_code == 422


async def test_answer_errors(client: AsyncClient, session: dict[str, Any]) -> None:
    qid = session["questions"][0]["id"]
    missing = await client.post(
        f"/sessions/{uuid.uuid4()}/questions/{qid}/answer", json={"text": ANSWER}
    )
    assert missing.json()["error"]["code"] == "session_not_found"
    foreign = await client.post(
        f"/sessions/{session['id']}/questions/{uuid.uuid4()}/answer", json={"text": ANSWER}
    )
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (
        404,
        "question_not_in_session",
    )

    await submit(client, session)
    again = await client.post(answer_url(session), json={"text": ANSWER})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_answered")

    await client.post(f"/sessions/{session['id']}/finish")
    finished = await client.post(answer_url(session, 2), json={"text": ANSWER})
    assert (finished.status_code, finished.json()["error"]["code"]) == (409, "session_finished")


async def test_answer_accepted_when_queue_is_down(
    client: AsyncClient, session: dict[str, Any], queue: Any
) -> None:
    queue.broken = True
    answer_id = await submit(client, session)
    answer = (await client.get(f"/answers/{answer_id}")).json()
    assert answer["status"] == "received"


async def test_evaluate_answer(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    engine: AsyncEngine,
    replies: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    answer_id = await submit(client, session)
    # модель «насчитала» бы что угодно — итог считает код: (0.3·3+0.25·2+0.2·1+0.15·2+0)/3
    llm.replies = [replies.evaluation([3, 2, 1, 2, 0], missed=[2, 9], wrong=[3])]

    with caplog.at_level(logging.INFO):
        await process_answer(deps, uuid.UUID(answer_id))

    request = llm.requests[0]
    assert session["questions"][0]["text"] in request.prompt
    assert f"<answer>\n{ANSWER}\n</answer>" in request.prompt
    assert (request.temperature, request.seed, request.max_tokens) == (0.0, 42, 1200)
    assert ANSWER not in caplog.text

    body = (await client.get(f"/answers/{answer_id}")).json()
    assert body["status"] == "done"
    evaluation = body["evaluation"]
    assert (evaluation["total"], evaluation["passed"]) == (63, True)
    assert [s["criterion"] for s in evaluation["scores"]] == load_rubric().ids
    assert evaluation["scores"][0] == {
        "criterion": "correctness",
        "title": "Корректность",
        "weight": 0.3,
        "score": 3,
        "reason": "обоснование correctness",
    }
    key_points = session["questions"][0]["key_points"]
    assert evaluation["missed_points"] == [key_points[1]]  # 9 — вне диапазона, выброшен
    assert "wrong_points" not in evaluation
    assert evaluation["model"] == "fake-model"

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT * FROM evaluation WHERE answer_id = :id"), {"id": answer_id}
            )
        ).one()
    assert row.status == "done"
    assert row.rubric_version == load_rubric().version
    assert row.prompt_version == load_prompt("evaluate_answer").version
    assert (row.provider, row.model, row.attempts) == ("fake", "fake-model", 1)
    assert row.params == {"temperature": 0.0, "seed": 42, "max_tokens": 1200, "num_ctx": 16384}
    assert row.raw_response == llm.replies[0]
    assert row.feedback["wrong_points"] == [key_points[2]]
    assert row.eval_run_id is None


async def test_bad_output_fails_answer(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    engine: AsyncEngine,
) -> None:
    answer_id = await submit(client, session)
    llm.replies = ["не json"]

    await process_answer(deps, uuid.UUID(answer_id))

    body = (await client.get(f"/answers/{answer_id}")).json()
    assert (body["status"], body["error"], body["evaluation"]) == (
        "failed",
        "llm_bad_output",
        None,
    )
    assert len(llm.requests) == 3
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT status, attempts, raw_response, total FROM evaluation"))
        ).one()
    assert (row.status, row.attempts, row.raw_response, row.total) == ("failed", 3, "не json", None)

    # после сбоя можно ответить заново
    again = await client.post(answer_url(session), json={"text": ANSWER})
    assert again.status_code == 202


async def test_llm_error_fails_answer(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, llm: FakeProvider
) -> None:
    answer_id = await submit(client, session)
    llm.replies = [LLMError("неверный ключ API")]
    await process_answer(deps, uuid.UUID(answer_id))
    body = (await client.get(f"/answers/{answer_id}")).json()
    assert (body["status"], body["error"]) == ("failed", "llm_error")


async def test_unavailable_llm_retries_then_fails(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    engine: AsyncEngine,
) -> None:
    answer_id = uuid.UUID(await submit(client, session))
    llm.available = False

    with pytest.raises(Retry) as info:
        await process_answer(deps, answer_id, job_try=1)
    assert info.value.defer_score == 10_000
    body = (await client.get(f"/answers/{answer_id}")).json()
    assert body["status"] == "evaluating"

    await process_answer(deps, answer_id, job_try=3)  # eval_max_tries = 3 в фикстуре
    body = (await client.get(f"/answers/{answer_id}")).json()
    assert (body["status"], body["error"]) == ("failed", "llm_unavailable")
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM evaluation")) == 0


async def test_evaluated_answer_is_not_evaluated_again(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, llm: FakeProvider, replies: Any
) -> None:
    answer_id = uuid.UUID(await submit(client, session))
    llm.replies = [replies.evaluation()]
    await process_answer(deps, answer_id)
    await process_answer(deps, answer_id)
    assert len(llm.requests) == 1


async def test_reevaluation_adds_row_and_live_is_latest(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    engine: AsyncEngine,
    replies: Any,
) -> None:
    answer_id = await submit(client, session)
    llm.replies = [replies.evaluation([1, 1, 1, 1, 1])]
    await process_answer(deps, uuid.UUID(answer_id))
    first = (await client.get(f"/answers/{answer_id}")).json()["evaluation"]

    # переоценка той же конфигурацией: ответ снова в очереди
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE answer SET status = 'received' WHERE id = :id"), {"id": answer_id}
        )
    llm.replies = [replies.evaluation([3, 3, 3, 3, 3])]
    await process_answer(deps, uuid.UUID(answer_id))

    second = (await client.get(f"/answers/{answer_id}")).json()["evaluation"]
    assert second["id"] != first["id"]
    assert (first["total"], second["total"]) == (33, 100)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM evaluation")) == 2


async def test_session_shows_answers_and_finish(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, llm: FakeProvider, replies: Any
) -> None:
    first = await submit(client, session, 1)
    second = await submit(client, session, 2)
    llm.replies = [replies.evaluation([3, 3, 3, 3, 3]), replies.evaluation([0, 0, 0, 0, 0])]
    await process_answer(deps, uuid.UUID(first))

    body = (await client.get(f"/sessions/{session['id']}")).json()
    answers = [q["answer"] for q in body["questions"]]
    assert answers[0] == {
        "id": first,
        "kind": "text",
        "status": "done",
        "total": 100,
        "passed": True,
    }
    assert answers[1] == {
        "id": second,
        "kind": "text",
        "status": "received",
        "total": None,
        "passed": None,
    }
    assert answers[2] is None

    summary = (await client.post(f"/sessions/{session['id']}/finish")).json()
    assert summary["status"] == "finished"
    assert summary["finished_at"] is not None
    assert (summary["answered"], summary["evaluated"], summary["passed"]) == (2, 1, 1)
    assert summary["avg_total"] == 100
    assert [q["answer_status"] for q in summary["questions"]] == ["done", "received", None]

    # ответ, принятый до завершения, дооценивается; повторный finish обновляет сводку
    await process_answer(deps, uuid.UUID(second))
    again = (await client.post(f"/sessions/{session['id']}/finish")).json()
    assert again["finished_at"] == summary["finished_at"]
    assert (again["evaluated"], again["passed"], again["avg_total"]) == (2, 1, 50)


async def test_finish_unknown_session(client: AsyncClient) -> None:
    response = await client.post(f"/sessions/{uuid.uuid4()}/finish")
    assert response.status_code == 404


async def test_get_unknown_answer(client: AsyncClient) -> None:
    response = await client.get(f"/answers/{uuid.uuid4()}")
    assert (response.status_code, response.json()["error"]["code"]) == (404, "answer_not_found")


async def test_recover_enqueues_stuck(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    queue: Any,
    replies: Any,
) -> None:
    stuck = uuid.UUID(await submit(client, session, 1))
    done = uuid.UUID(await submit(client, session, 2))
    llm.replies = [replies.evaluation()]
    await process_answer(deps, done)
    queue.evaluations.clear()

    await recover(deps, queue)

    assert queue.evaluations == [stuck]
