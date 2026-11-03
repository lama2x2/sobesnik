"""Прогресс по темам, выбор тем, повторение и удаление всех данных (007)."""

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.db.session import make_sessionmaker
from sobesnik_api.services.progress import rebuild
from sobesnik_api.worker import WorkerDeps, process_answer
from sobesnik_asr.samples import ogg_opus_bytes
from sobesnik_core.rubric import load_rubric
from sobesnik_llm import FakeProvider

from .conftest import questions_reply

LOW, MID, HIGH = 0, 2, 3
"""Балл по всем критериям → итог 0, 67, 100."""
TOTAL = {LOW: 0, MID: 67, HIGH: 100}
N_CRITERIA = len(load_rubric().ids)


async def answer(
    client: AsyncClient,
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    session: dict[str, Any],
    position: int,
    score: int,
) -> str:
    qid = session["questions"][position - 1]["id"]
    response = await client.post(
        f"/sessions/{session['id']}/questions/{qid}/answer", json={"text": "ответ"}
    )
    assert response.status_code == 202, response.text
    answer_id: str = response.json()["answer_id"]
    llm.replies = [replies.evaluation([score] * N_CRITERIA)]
    await process_answer(deps, uuid.UUID(answer_id))
    return answer_id


async def new_session(
    client: AsyncClient, llm: FakeProvider, body: dict[str, Any], n: int = 3
) -> dict[str, Any]:
    llm.replies = [questions_reply(n, prefix=f"Вопрос {uuid.uuid4().hex[:6]}")]
    response = await client.post("/sessions", json={"n": n} | body)
    assert response.status_code == 201, response.text
    llm.replies = []
    data: dict[str, Any] = response.json()
    return data


async def progress(client: AsyncClient, user: dict[str, Any]) -> dict[str, Any]:
    response = await client.get(f"/users/{user['id']}/progress")
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


def topic(data: dict[str, Any], slug: str) -> dict[str, Any]:
    return next(t for t in data["topics"] if t["slug"] == slug)


async def test_progress_after_evaluation(
    client: AsyncClient,
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    user: dict[str, Any],
    session: dict[str, Any],
) -> None:
    assert [q["topic"] for q in session["questions"]] == [
        "python.asyncio",
        "db.indexes",
        "devops.kubernetes",
    ]
    await answer(client, deps, llm, replies, session, 1, LOW)
    await answer(client, deps, llm, replies, session, 2, HIGH)

    data = await progress(client, user)
    asyncio_ = topic(data, "python.asyncio")
    assert asyncio_ | {"docs": None} == {
        "slug": "python.asyncio",
        "title": "asyncio и асинхронный код",
        "status": "few",
        "low": True,
        "attempts": 1,
        "last_score": 0,
        "avg_last3": 0.0,
        "docs": None,
    }
    assert asyncio_["docs"][0]["url"].startswith("https://docs.python.org/")
    assert topic(data, "db.indexes")["low"] is False
    assert [t["slug"] for t in data["topics"]] == ["python.asyncio", "db.indexes"]
    assert data["thresholds"] == {"min_attempts": 2, "weak_avg": 60.0, "low_score": 60.0}
    assert data["sessions"][0]["scores"] == [0, 100, None]
    assert data["sessions"][0]["mode"] == "vacancy"
    assert data["sessions"][0]["vacancy_title"] == "Python-разработчик"


async def test_recompute_is_idempotent_and_ignores_other_evaluations(
    client: AsyncClient,
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    user: dict[str, Any],
    session: dict[str, Any],
    engine: AsyncEngine,
) -> None:
    answer_id = await answer(client, deps, llm, replies, session, 1, LOW)
    async with engine.begin() as conn:
        # повтор задачи и «переоценка» в обход: состояние ответа снова evaluating
        await conn.execute(
            text("UPDATE answer SET status = 'evaluating' WHERE id = :id"), {"id": answer_id}
        )
    llm.replies = [replies.evaluation([HIGH] * N_CRITERIA)]
    await process_answer(deps, uuid.UUID(answer_id))
    async with engine.begin() as conn:
        # оценка прогона в прогресс не идёт
        await conn.execute(
            text(
                "INSERT INTO eval_run (id, name, provider, model, rubric_version, prompt_version,"
                " filter, status, total, done, failed) VALUES (:id, 'r', 'fake', 'm', 'v', 'p',"
                " '{}', 'done', 1, 1, 0)"
            ),
            {"id": uuid.uuid4()},
        )
        await conn.execute(
            text(
                "INSERT INTO evaluation (id, answer_id, eval_run_id, rubric_version, provider,"
                " model, prompt_version, params, total, passed, attempts, latency_ms, status)"
                " SELECT :id, :answer, (SELECT id FROM eval_run LIMIT 1), 'v', 'fake', 'm', 'p',"
                " '{}', 5, false, 1, 1, 'done'"
            ),
            {"id": uuid.uuid4(), "answer": answer_id},
        )

    data = await progress(client, user)
    stats = topic(data, "python.asyncio")
    assert (stats["attempts"], stats["last_score"]) == (1, 100)  # последняя боевая оценка

    # пропуск и неудачная оценка — не попытки
    qid = session["questions"][1]["id"]
    failed = await client.post(
        f"/sessions/{session['id']}/questions/{qid}/answer", json={"text": "ответ"}
    )
    llm.replies = ["не json"] * 5
    await process_answer(deps, uuid.UUID(failed.json()["answer_id"]))
    assert [t["slug"] for t in (await progress(client, user))["topics"]] == ["python.asyncio"]

    async with make_sessionmaker(engine)() as db:
        assert await rebuild(db) == 1
    assert (await progress(client, user))["topics"] == data["topics"]


async def test_status_changes_with_attempts(
    client: AsyncClient,
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    user: dict[str, Any],
    vacancy: dict[str, Any],
) -> None:
    body = {"vacancy_id": vacancy["id"], "n": 3, "topics": ["db.indexes"]}
    for score, status in [(LOW, "few"), (LOW, "weak"), (HIGH, "weak"), (HIGH, "ok")]:
        session = await new_session(client, llm, body)
        await answer(client, deps, llm, replies, session, 1, score)
        assert topic(await progress(client, user), "db.indexes")["status"] == status


async def test_topics_filter_plan(
    client: AsyncClient, llm: FakeProvider, vacancy: dict[str, Any]
) -> None:
    session = await new_session(
        client, llm, {"vacancy_id": vacancy["id"], "n": 3, "topics": ["devops.kubernetes"]}
    )
    assert {q["topic"] for q in session["questions"]} == {"devops.kubernetes"}
    assert session["topics"] == ["devops.kubernetes"]
    assert "Kubernetes" in llm.requests[-1].prompt
    assert "Asyncio" not in llm.requests[-1].prompt

    response = await client.post(
        "/sessions", json={"vacancy_id": vacancy["id"], "topics": ["go.types"]}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (
        422,
        "topics_not_in_vacancy",
    )
    empty = await client.post("/sessions", json={"vacancy_id": vacancy["id"], "topics": []})
    assert empty.status_code == 422


async def test_session_questions_have_docs(session: dict[str, Any]) -> None:
    first = session["questions"][0]
    assert first["topic"] == "python.asyncio"
    assert 1 <= len(first["docs"]) <= 2
    assert first["docs"][0]["url"].startswith("https://docs.python.org/")


async def test_requirement_topic_title(vacancy: dict[str, Any]) -> None:
    assert vacancy["requirements"][0]["topic_title"] == "asyncio и асинхронный код"


async def test_repeat_session(
    client: AsyncClient,
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    user: dict[str, Any],
    session: dict[str, Any],
) -> None:
    await answer(client, deps, llm, replies, session, 1, 1)  # 33: мало данных, низкий
    await answer(client, deps, llm, replies, session, 2, HIGH)  # мало данных, высокий
    await answer(client, deps, llm, replies, session, 3, MID)
    second = await new_session(
        client, llm, {"vacancy_id": session["vacancy_id"], "topics": ["devops.kubernetes"]}
    )
    await answer(client, deps, llm, replies, second, 1, LOW)  # 67, 0 → 33,5 — слабая

    repeat = await new_session(client, llm, {"mode": "repeat", "user_id": user["id"], "n": 5}, n=2)
    assert repeat["mode"] == "repeat"
    assert repeat["vacancy_id"] is None
    assert [q["topic"] for q in repeat["questions"]] == ["devops.kubernetes", "python.asyncio"]
    assert repeat["questions"][0]["requirement_id"] == session["questions"][2]["requirement_id"]
    prompt = llm.requests[-1].prompt
    assert "Повторение слабых тем" in prompt
    assert session["questions"][0]["text"] in prompt  # прежние вопросы — в «уже задавались»

    assert "Уровень: middle" in prompt

    harder = await new_session(
        client, llm, {"mode": "repeat", "user_id": user["id"], "n": 5, "difficulty": "harder"}, n=2
    )
    assert harder["difficulty"] == "harder"
    assert "Уровень: senior" in llm.requests[-1].prompt
    await client.post(f"/sessions/{harder['id']}/finish")

    # ответ хороший: 33 и 100 → 66,5 — тема из «мало данных» уходит в «в порядке»
    await answer(client, deps, llm, replies, repeat, 2, HIGH)
    assert topic(await progress(client, user), "python.asyncio")["status"] == "ok"
    data = await progress(client, user)
    assert data["sessions"][0]["mode"] == "repeat"
    assert data["sessions"][0]["vacancy_title"] is None


async def test_repeat_errors(client: AsyncClient, user: dict[str, Any]) -> None:
    nothing = await client.post("/sessions", json={"mode": "repeat", "user_id": user["id"]})
    assert (nothing.status_code, nothing.json()["error"]["code"]) == (409, "nothing_to_repeat")
    unknown = await client.post("/sessions", json={"mode": "repeat", "user_id": str(uuid.uuid4())})
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "user_not_found")
    assert (await client.post("/sessions", json={"mode": "repeat"})).status_code == 422
    required = {"mode": "repeat", "user_id": user["id"], "required_only": True}
    assert (await client.post("/sessions", json=required)).status_code == 422
    assert (await client.post("/sessions", json={"n": 3})).status_code == 422
    assert (await client.get(f"/users/{uuid.uuid4()}/progress")).status_code == 404


async def test_answer_has_topic_docs(
    client: AsyncClient,
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    session: dict[str, Any],
) -> None:
    answer_id = await answer(client, deps, llm, replies, session, 2, LOW)
    body = (await client.get(f"/answers/{answer_id}")).json()
    assert body["topic"] == "db.indexes"
    assert 1 <= len(body["docs"]) <= 2
    assert body["docs"][0] == {
        "title": "PostgreSQL: индексы",
        "url": "https://postgrespro.ru/docs/postgresql/17/indexes",
    }


async def test_empty_progress(client: AsyncClient, user: dict[str, Any]) -> None:
    assert (await progress(client, user))["topics"] == []


async def _counts(engine: AsyncEngine, user_id: str) -> dict[str, int]:
    queries = {
        "vacancy": "SELECT count(*) FROM vacancy WHERE user_id = :u",
        "session": "SELECT count(*) FROM session WHERE user_id = :u",
        "answer": "SELECT count(*) FROM answer WHERE user_id = :u",
        "progress": "SELECT count(*) FROM topic_progress WHERE user_id = :u",
        "audio": "SELECT count(*) FROM audio a JOIN answer s ON s.id = a.answer_id"
        " WHERE s.user_id = :u",
    }
    async with engine.connect() as conn:
        return {k: int(await conn.scalar(text(q), {"u": user_id}) or 0) for k, q in queries.items()}


async def test_forget_all(
    client: AsyncClient,
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    user: dict[str, Any],
    session: dict[str, Any],
    storage: Any,
    engine: AsyncEngine,
    profile: dict[str, Any],
) -> None:
    await answer(client, deps, llm, replies, session, 1, LOW)
    qid = session["questions"][1]["id"]
    voice = await client.post(
        f"/sessions/{session['id']}/questions/{qid}/answer/voice",
        files={"file": ("a.ogg", ogg_opus_bytes(2.0), "audio/ogg")},
    )
    assert voice.status_code == 202, voice.text

    # чужие данные: другой пользователь со своей вакансией и прогрессом
    other = (await client.put("/users/telegram/200")).json()
    llm.replies = [replies.parse(profile)]
    other_vacancy = (
        await client.post("/vacancies", json={"user_id": other["id"], "text": "x" * 200})
    ).json()
    other_session = await new_session(client, llm, {"vacancy_id": other_vacancy["id"]})
    await answer(client, deps, llm, replies, other_session, 1, LOW)
    other_before = await _counts(engine, other["id"])

    storage.available = False
    failed = await client.delete(f"/users/{user['id']}/data?scope=all")
    assert failed.status_code == 503
    assert (await _counts(engine, user["id"]))["answer"] == 2
    storage.available = True

    response = await client.delete(f"/users/{user['id']}/data?scope=all")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "audio_deleted": 1,
        "transcripts_deleted": 0,
        "reviews_deleted": 0,
        "answers_deleted": 2,
        "sessions_deleted": 1,
        "vacancies_deleted": 1,
    }
    assert await _counts(engine, user["id"]) == dict.fromkeys(other_before, 0)
    assert f"audio/{voice.json()['answer_id']}" not in storage.objects
    assert await _counts(engine, other["id"]) == other_before
    assert (await progress(client, user))["topics"] == []
    assert (await client.put("/users/telegram/100")).json()["id"] == user["id"]
