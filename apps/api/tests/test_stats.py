"""Сводка времени операций по БД (011 §7)."""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from sobesnik_api.cli import build_parser, run_command
from sobesnik_api.services.stats import format_table, latency_stats, percentile, summarize
from sobesnik_api.worker import WorkerDeps, process_answer
from sobesnik_llm import FakeProvider

from .conftest import FakeQueue


def test_summarize() -> None:
    s = summarize("оценка", [20.0, 10.0, 30.0, 40.0])
    assert (s.count, s.median_s, s.p90_s, s.min_s, s.max_s) == (4, 25.0, 40.0, 10.0, 40.0)
    assert summarize("пусто", []).median_s is None
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9
    table = format_table([summarize("оценка", [1.5]), summarize("разбор", [])])
    assert "| оценка | 1 | 1,5 | 1,5 | 1,5–1,5 |" in table
    assert "| разбор | 0 | — | — | — |" in table


async def test_latency_stats_from_db(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    queue: FakeQueue,
    capsys: pytest.CaptureFixture[str],
) -> None:
    qid = session["questions"][0]["id"]
    response = await client.post(
        f"/sessions/{session['id']}/questions/{qid}/answer", json={"text": "ответ"}
    )
    llm.replies = [replies.evaluation()]
    await process_answer(deps, uuid.UUID(response.json()["answer_id"]))

    async with deps.sessionmaker() as db:
        stats = {s.name: s for s in await latency_stats(db)}
        assert stats["разбор вакансии"].count == 1
        assert stats["генерация вопросов, сессия"].count == 1
        assert stats["генерация, на 1 вопрос"].count == 1
        assert stats["оценка ответа"].count == 1
        assert stats["распознавание, на 1 мин речи"].count == 0
        assert stats["подробный разбор"].count == 0

        args = build_parser().parse_args(["stats", "--since", "2100-01-01"])
        assert await run_command(args, deps.settings, db, queue) == 0
    out = capsys.readouterr().out
    assert "| разбор вакансии | 0 | — |" in out
