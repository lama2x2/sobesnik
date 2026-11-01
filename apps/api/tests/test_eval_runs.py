"""Прогоны оценки, выгрузка CSV и CLI (003 §9)."""

import csv
import io
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.cli import build_parser, run_command
from sobesnik_api.db.models import EvalRun
from sobesnik_api.export import COMMON, TAIL, ExportFilter, select_rows, write_csv
from sobesnik_api.services.eval_runs import RunFilter, create_run, get_run
from sobesnik_api.settings import Settings
from sobesnik_api.worker import WorkerDeps, process_answer, process_run
from sobesnik_core.rubric import load_rubric
from sobesnik_llm import FakeProvider

CRITERIA = load_rubric().ids


async def answer_all(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, llm: FakeProvider, reply: str
) -> list[uuid.UUID]:
    """Ответы на все вопросы сессии с «боевыми» оценками."""
    ids = []
    llm.replies = [reply]
    for question in session["questions"]:
        response = await client.post(
            f"/sessions/{session['id']}/questions/{question['id']}/answer",
            json={"text": f"Ответ на «{question['text']}»"},
        )
        answer_id = uuid.UUID(response.json()["answer_id"])
        await process_answer(deps, answer_id)
        ids.append(answer_id)
    llm.requests.clear()
    return ids


async def new_run(
    deps: WorkerDeps, model: str = "small", run_filter: RunFilter | None = None
) -> EvalRun:
    async with deps.sessionmaker() as db:
        return await create_run(
            db, name="тест", provider="fake", model=model, run_filter=run_filter or RunFilter()
        )


async def reload(deps: WorkerDeps, run_id: uuid.UUID) -> EvalRun:
    async with deps.sessionmaker() as db:
        return await get_run(db, run_id)


async def test_run_with_other_model(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    run_models: Any,
    replies: Any,
    engine: AsyncEngine,
) -> None:
    answers = await answer_all(client, session, deps, llm, replies.evaluation([1] * 5))
    llm.replies = [replies.evaluation([3] * 5)]
    run = await new_run(deps, run_filter=RunFilter(session_id=uuid.UUID(session["id"])))

    await process_run(deps, run.id)

    run = await reload(deps, run.id)
    assert (run.status, run.total, run.done, run.failed) == ("done", 3, 3, 0)
    assert run.finished_at is not None
    assert run_models.opened == ["small"]
    assert len(run_models.providers["small"].requests) == 3
    assert llm.requests == []  # «боевая» модель не вызывалась
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT model, total FROM evaluation WHERE eval_run_id = :id"), {"id": run.id}
            )
        ).all()
    assert {(r.model, r.total) for r in rows} == {("small", 100)}

    # «боевая» оценка и статус ответа не изменились
    body = (await client.get(f"/answers/{answers[0]}")).json()
    assert (body["status"], body["evaluation"]["total"]) == ("done", 33)


async def test_restart_skips_done(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    run_models: Any,
    replies: Any,
) -> None:
    await answer_all(client, session, deps, llm, replies.evaluation())
    llm.replies = [replies.evaluation(), "не json"]  # первый ответ оценён, дальше сбои
    run = await new_run(deps)

    await process_run(deps, run.id)
    run = await reload(deps, run.id)
    assert (run.status, run.done, run.failed) == ("done", 1, 2)

    llm.replies = [replies.evaluation()]
    await process_run(deps, run.id)

    run = await reload(deps, run.id)
    assert (run.status, run.done, run.failed) == ("done", 3, 0)
    # 1 + 2×3 попытки в первом проходе и 2 во втором: готовый ответ пропущен
    assert len(run_models.providers["small"].requests) == 7 + 2


async def test_run_stops_when_llm_unavailable(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, llm: FakeProvider, replies: Any
) -> None:
    await answer_all(client, session, deps, llm, replies.evaluation())
    llm.available = False
    run = await new_run(deps)

    await process_run(deps, run.id)

    run = await reload(deps, run.id)
    assert (run.status, run.done, run.failed) == ("failed", 0, 3)
    assert run.error is not None and "LLM недоступна" in run.error


async def test_run_filter(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    queue: Any,
    replies: Any,
) -> None:
    await answer_all(client, session, deps, llm, replies.evaluation())
    llm.replies = [replies.evaluation()]
    tomorrow = date.today() + timedelta(days=1)

    future = await new_run(deps, run_filter=RunFilter(since=tomorrow))
    await process_run(deps, future.id)
    assert (await reload(deps, future.id)).total == 0

    other = await new_run(deps, run_filter=RunFilter(session_id=uuid.uuid4()))
    await process_run(deps, other.id)
    assert (await reload(deps, other.id)).total == 0

    # ответ, который ещё оценивается, в прогон не попадает
    await client.post(f"/sessions/{session['id']}/finish")
    today = await new_run(deps, run_filter=RunFilter(since=date.today(), until=date.today()))
    await process_run(deps, today.id)
    assert (await reload(deps, today.id)).total == 3


async def test_run_refuses_changed_rubric(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, engine: AsyncEngine
) -> None:
    run = await new_run(deps)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE eval_run SET rubric_version = 'v1#00000000' WHERE id = :id"),
            {"id": run.id},
        )

    await process_run(deps, run.id)

    run = await reload(deps, run.id)
    assert run.status == "failed"
    assert run.error is not None and "изменились" in run.error


def read_csv(content: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content)))


async def test_export_wide_and_long(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
) -> None:
    answers = await answer_all(client, session, deps, llm, replies.evaluation([1] * 5))
    llm.replies = [replies.evaluation([3] * 5), "не json"]
    run = await new_run(deps)
    await process_run(deps, run.id)

    async with deps.sessionmaker() as db:
        rows = await select_rows(db, ExportFilter(run_ids=[run.id], live=True))
    assert len(rows) == 6

    wide = io.StringIO()
    assert write_csv(wide, rows, "wide") == 6
    data = read_csv(wide.getvalue())
    columns = list(data[0])
    assert columns[: len(COMMON)] == COMMON
    assert columns[-len(TAIL) :] == TAIL
    assert columns[len(COMMON) : len(COMMON) + 2] == ["correctness_score", "correctness_reason"]
    # оценки одного ответа стоят рядом: сначала «боевая», потом прогона
    assert [r["answer_id"] for r in data] == [str(a) for a in answers for _ in range(2)]
    assert [r["model"] for r in data[:2]] == ["fake-model", "small"]
    assert [r["run_name"] for r in data[:2]] == ["", "тест"]
    assert (data[0]["total"], data[1]["total"]) == ("33", "100")
    assert data[0]["key_points"] == " | ".join(session["questions"][0]["key_points"])
    assert data[0]["reference_answer"] == data[0]["human_score"] == ""
    failed = data[3]
    assert (failed["status"], failed["error"], failed["total"]) == (
        "failed",
        "llm_bad_output",
        "",
    )
    assert failed["correctness_score"] == ""

    long = io.StringIO()
    assert write_csv(long, rows, "long") == 6 * len(CRITERIA)
    long_rows = read_csv(long.getvalue())
    assert [r["criterion"] for r in long_rows[:5]] == CRITERIA
    assert long_rows[0]["weight"] == "0.3"
    assert long_rows[0]["score"] == "1"


async def test_cli(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    queue: Any,
    replies: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await answer_all(client, session, deps, llm, replies.evaluation())
    settings = Settings(llm_eval_model="eval-model")
    parser = build_parser()

    async def cli(*argv: str) -> int:
        async with deps.sessionmaker() as db:
            return await run_command(parser.parse_args(argv), settings, db, queue)

    assert await cli("eval-run", "create", "--name", "base", "--session", session["id"]) == 0
    run_id = queue.runs[-1]
    async with deps.sessionmaker() as db:
        run = await get_run(db, run_id)
    assert (run.model, run.status, run.filter) == (
        "eval-model",
        "queued",
        {"session_id": session["id"]},
    )

    llm.replies = [replies.evaluation()]
    await process_run(deps, run_id)
    assert await cli("eval-run", "status", str(run_id)) == 0
    assert "готово 3/3" in capsys.readouterr().out

    assert await cli("eval-run", "restart", str(run_id)) == 0
    assert queue.runs == [run_id, run_id]

    out = tmp_path / "eval.csv"
    assert await cli("export", "--run", str(run_id), "--live", "-o", str(out)) == 0
    content = out.read_bytes()
    assert content.startswith(b"\xef\xbb\xbf")  # BOM для Excel
    assert len(read_csv(content.decode("utf-8-sig"))) == 6

    assert await cli("export", "-o", str(out)) == 2
