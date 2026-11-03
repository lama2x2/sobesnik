"""Подробный разбор ответа по кнопке (010 §4)."""

import asyncio
import json
import logging
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.worker import WorkerDeps, process_answer, process_transcription
from sobesnik_asr.samples import ogg_opus_bytes
from sobesnik_llm import FakeProvider, LLMUnavailableError

ANSWER = "Индекс упорядочен по первому столбцу. Индекс всегда ускоряет любые запросы."


def review_reply(
    *,
    points: list[dict[str, Any]] | None = None,
    mistakes: list[dict[str, str]] | None = None,
    issue: str = "",
) -> str:
    return json.dumps(
        {
            "question_issue": issue,
            "reference": "Сильный ответ: индекс упорядочен по первому столбцу.",
            "points": points
            if points is not None
            else [{"n": n, "comment": f"комментарий {n}"} for n in (1, 2, 3)],
            "mistakes": mistakes or [],
        },
        ensure_ascii=False,
    )


async def evaluated(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    position: int = 1,
    **evaluation: Any,
) -> str:
    qid = session["questions"][position - 1]["id"]
    response = await client.post(
        f"/sessions/{session['id']}/questions/{qid}/answer", json={"text": ANSWER}
    )
    answer_id: str = response.json()["answer_id"]
    llm.replies = [replies.evaluation(**evaluation)]
    await process_answer(deps, uuid.UUID(answer_id))
    llm.requests.clear()
    return answer_id


async def test_review(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    answer_id = await evaluated(client, session, deps, llm, replies, missed=[2], wrong=[3])
    assert (await client.get(f"/answers/{answer_id}")).json()["has_review"] is False
    assert (await client.get(f"/answers/{answer_id}/review")).status_code == 404
    llm.replies = [
        review_reply(
            mistakes=[
                {"quote": "индекс всегда ускоряет", "correction": "Не всегда: запись медленнее."},
                {"quote": "индекс замедляет чтение", "correction": "этого в ответе нет"},
            ],
            issue="нет",
        )
    ]

    with caplog.at_level(logging.INFO):
        response = await client.post(f"/answers/{answer_id}/review")

    assert response.status_code == 200, response.text
    body = response.json()
    key_points = session["questions"][0]["key_points"]
    assert body["cached"] is False
    assert body["reference"].startswith("Сильный ответ")
    assert body["points"] == [
        {"n": 1, "text": key_points[0], "status": "covered", "comment": "комментарий 1"},
        {"n": 2, "text": key_points[1], "status": "missed", "comment": "комментарий 2"},
        {"n": 3, "text": key_points[2], "status": "missed", "comment": "комментарий 3"},
    ]
    assert body["mistakes"] == [
        {"quote": "индекс всегда ускоряет", "correction": "Не всегда: запись медленнее."}
    ]
    assert body["question_issue"] is None
    assert [d["url"] for d in body["docs"]][:1] == [
        "https://docs.python.org/3/library/asyncio.html"
    ]
    assert body["model"] == "fake-model"

    request = llm.requests[0]
    assert f"1. [прозвучал в ответе] {key_points[0]}" in request.prompt
    assert f"2. [не прозвучал в ответе или сказан неверно] {key_points[1]}" in request.prompt
    assert f"<answer>\n{ANSWER}\n</answer>" in request.prompt
    assert (request.temperature, request.seed) == (0.0, 42)
    assert "индекс всегда" not in caplog.text and "Сильный ответ" not in caplog.text
    assert "answer reviewed" in caplog.text

    # второе нажатие и GET — из БД, без модели
    llm.requests.clear()
    again = (await client.post(f"/answers/{answer_id}/review")).json()
    stored = (await client.get(f"/answers/{answer_id}/review")).json()
    assert llm.requests == []
    assert again["cached"] is True and stored["cached"] is True
    assert again["points"] == body["points"] == stored["points"]
    assert (await client.get(f"/answers/{answer_id}")).json()["has_review"] is True


async def test_question_issue_kept(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
) -> None:
    answer_id = await evaluated(client, session, deps, llm, replies)
    issue = "В индексе (id, status) первый столбец — id, а не status."
    llm.replies = [review_reply(issue=issue, points=[{"n": 9, "comment": "лишний"}])]
    body = (await client.post(f"/answers/{answer_id}/review")).json()
    assert body["question_issue"] == issue
    assert [p["comment"] for p in body["points"]] == ["", "", ""]


async def test_parallel_clicks_store_one_review(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    engine: AsyncEngine,
) -> None:
    answer_id = await evaluated(client, session, deps, llm, replies)
    llm.replies = [review_reply()]
    first, second = await asyncio.gather(
        client.post(f"/answers/{answer_id}/review"), client.post(f"/answers/{answer_id}/review")
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["reference"] == second.json()["reference"]
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM answer_review")) == 1


async def test_reevaluation_gets_new_review(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    engine: AsyncEngine,
) -> None:
    answer_id = await evaluated(client, session, deps, llm, replies)
    llm.replies = [review_reply()]
    first = (await client.post(f"/answers/{answer_id}/review")).json()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE answer SET status = 'evaluating' WHERE id = :id"), {"id": answer_id}
        )
    llm.replies = [replies.evaluation(missed=[1])]
    await process_answer(deps, uuid.UUID(answer_id))
    assert (await client.get(f"/answers/{answer_id}")).json()["has_review"] is False
    llm.replies = [review_reply()]
    second = (await client.post(f"/answers/{answer_id}/review")).json()
    assert second["evaluation_id"] != first["evaluation_id"]
    assert second["points"][0]["status"] == "missed"


async def test_review_errors(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    engine: AsyncEngine,
) -> None:
    missing = await client.post(f"/answers/{uuid.uuid4()}/review")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "answer_not_found")

    qid = session["questions"][0]["id"]
    pending = (
        await client.post(
            f"/sessions/{session['id']}/questions/{qid}/answer", json={"text": ANSWER}
        )
    ).json()["answer_id"]
    not_yet = await client.post(f"/answers/{pending}/review")
    assert (not_yet.status_code, not_yet.json()["error"]["code"]) == (409, "answer_not_evaluated")

    answer_id = await evaluated(client, session, deps, llm, replies, position=2)
    llm.replies = ["не json"]
    bad = await client.post(f"/answers/{answer_id}/review")
    assert (bad.status_code, bad.json()["error"]["code"]) == (502, "llm_bad_output")
    llm.replies = [LLMUnavailableError("нет связи")]
    down = await client.post(f"/answers/{answer_id}/review")
    assert down.status_code == 503
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM answer_review")) == 0


async def test_foreign_answer_is_404(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
) -> None:
    answer_id = await evaluated(client, session, deps, llm, replies)
    stranger = (await client.put("/users/telegram/200")).json()
    link = (await client.post("/auth/links", json={"user_id": stranger["id"]})).json()
    token = (await client.post("/auth/links/redeem", json={"secret": link["secret"]})).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    for response in (
        await client.post(f"/answers/{answer_id}/review", headers=headers),
        await client.get(f"/answers/{answer_id}/review", headers=headers),
    ):
        assert (response.status_code, response.json()["error"]["code"]) == (
            404,
            "answer_not_found",
        )


async def test_forget_deletes_reviews(
    client: AsyncClient,
    session: dict[str, Any],
    user: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    engine: AsyncEngine,
) -> None:
    text_id = await evaluated(client, session, deps, llm, replies)
    qid = session["questions"][1]["id"]
    files = {"file": ("голос.ogg", ogg_opus_bytes(2.0), "audio/ogg")}
    voice = await client.post(
        f"/sessions/{session['id']}/questions/{qid}/answer/voice", files=files
    )
    voice_id = uuid.UUID(voice.json()["answer_id"])
    llm.replies = [replies.evaluation()]
    await process_transcription(deps, voice_id)
    await process_answer(deps, voice_id)
    llm.replies = [review_reply()]
    voice_review = await client.post(f"/answers/{voice_id}/review")
    assert voice_review.status_code == 200, voice_review.text
    assert "Составной индекс" in llm.requests[-1].prompt  # разбирается расшифровка
    await client.post(f"/answers/{text_id}/review")

    audio = (await client.delete(f"/users/{user['id']}/data?scope=audio")).json()
    assert audio["reviews_deleted"] == 1
    gone = await client.post(f"/answers/{voice_id}/review")
    assert (gone.status_code, gone.json()["error"]["code"]) == (409, "transcript_deleted")
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM answer_review")) == 1

    await client.delete(f"/users/{user['id']}/data?scope=all")
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM answer_review")) == 0
