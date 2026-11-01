"""Импорт открытых датасетов, прогоны по ним и выгрузка с человеческой оценкой (005 §6–9)."""

import csv
import io
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.cli import build_parser, run_command
from sobesnik_api.db.models import Answer, Dataset, EvalRun, Evaluation, Question
from sobesnik_api.errors import AppError
from sobesnik_api.export import COMMON, ExportFilter, select_rows, write_csv
from sobesnik_api.services.datasets import import_dataset, list_datasets
from sobesnik_api.services.eval_runs import RunFilter, create_run, get_run
from sobesnik_api.services.voice import AudioLimits
from sobesnik_api.worker import WorkerDeps, process_run, process_transcription, recover
from sobesnik_asr.samples import ogg_opus_bytes
from sobesnik_core.dataset import parse_jsonl
from sobesnik_llm import FakeProvider

LIMITS = AudioLimits(max_bytes=1024 * 1024, max_seconds=300, ttl_days=30)
META = {
    "name": "mohler",
    "license": "CC-BY-4.0",
    "lang": "en",
    "score_key": "avg",
    "score_min": 0,
    "score_max": 5,
    "source_url": "https://example.org/mohler",
}


def text_item(n: int, score: float) -> dict[str, Any]:
    return {
        "external_id": f"E01.Q0{n % 2}.A{n}",
        "question_id": f"E01.Q0{n % 2}",
        "question": f"Question {n % 2}?",
        "reference_answer": f"Reference {n % 2}.",
        "answer": f"Student answer {n}",
        "human_scores": {"grader_1": score, "grader_2": score, "avg": score},
    }


def write_jsonl(path: Path, meta: dict[str, Any], rows: list[dict[str, Any]]) -> Path:
    lines = [json.dumps({"dataset": meta}), *(json.dumps(r) for r in rows)]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


async def do_import(deps: WorkerDeps, path: Path, **kwargs: Any) -> tuple[uuid.UUID, int]:
    with path.open(encoding="utf-8") as f:
        parsed = parse_jsonl(f)
    async with deps.sessionmaker() as db:
        result = await import_dataset(
            db, deps.queue, deps.storage, parsed, base_dir=path.parent, limits=LIMITS, **kwargs
        )
        return result.dataset.id, result.answers


async def mohler(deps: WorkerDeps, tmp_path: Path, n: int = 4) -> uuid.UUID:
    rows = [text_item(i, score=i % 6) for i in range(n)]
    dataset_id, _ = await do_import(deps, write_jsonl(tmp_path / "mohler.jsonl", META, rows))
    return dataset_id


async def test_import_text(deps: WorkerDeps, tmp_path: Path, engine: AsyncEngine) -> None:
    await mohler(deps, tmp_path)
    async with deps.sessionmaker() as db:
        questions = list(await db.scalars(select(Question).order_by(Question.external_id)))
        answers = list(await db.scalars(select(Answer)))
        dataset = await db.scalar(select(Dataset))
    assert dataset is not None
    assert (dataset.name, dataset.score_max, dataset.license) == ("mohler", 5, "CC-BY-4.0")
    assert [q.external_id for q in questions] == ["E01.Q00", "E01.Q01"]  # без дублей
    assert {q.source for q in questions} == {"dataset"}
    assert questions[0].reference_answer == "Reference 0."
    assert len(answers) == 4
    assert {(a.source, a.kind, a.status) for a in answers} == {("dataset", "text", "imported")}
    assert all(a.user_id is None and a.session_id is None for a in answers)
    assert deps.queue.evaluations == []  # type: ignore[attr-defined]


async def test_import_twice_conflicts(deps: WorkerDeps, tmp_path: Path) -> None:
    await mohler(deps, tmp_path)
    with pytest.raises(AppError, match="уже импортирован"):
        await mohler(deps, tmp_path)


async def test_import_sample_named_by_size(deps: WorkerDeps, tmp_path: Path) -> None:
    rows = [text_item(i, score=i % 6) for i in range(12)]
    path = write_jsonl(tmp_path / "m.jsonl", META, rows)
    _, count = await do_import(deps, path, sample=6)
    assert count == 6
    async with deps.sessionmaker() as db:
        names = [s.dataset.name for s in await list_datasets(db)]
    assert names == ["mohler-6"]


async def test_import_voice(deps: WorkerDeps, tmp_path: Path, storage: Any, queue: Any) -> None:
    (tmp_path / "frag.ogg").write_bytes(ogg_opus_bytes())
    meta = {"name": "voice", "license": "private", "lang": "ru"}
    row = {
        "external_id": "f1",
        "question_id": "f1",
        "question": "Что такое GIL?",
        "audio": "frag.ogg",
    }
    await do_import(deps, write_jsonl(tmp_path / "voice.jsonl", meta, [row]))
    async with deps.sessionmaker() as db:
        answer = await db.scalar(select(Answer))
    assert answer is not None
    assert (answer.kind, answer.status) == ("voice", "received")
    assert queue.transcriptions == [answer.id]
    assert f"audio/{answer.id}" in storage.objects

    await process_transcription(deps, answer.id)
    async with deps.sessionmaker() as db:
        answer = await db.get(Answer, answer.id)
        summary = (await list_datasets(db))[0]
    assert answer is not None and answer.status == "imported"
    assert queue.evaluations == []  # «боевой» оценки у ответа датасета нет
    assert (summary.voice, summary.transcribed) == (1, 1)

    queue.transcriptions.clear()
    await recover(deps, queue)
    assert queue.transcriptions == queue.evaluations == []


@pytest.mark.parametrize("audio", ["../outside.ogg", "missing.ogg"])
async def test_import_bad_audio_writes_nothing(
    deps: WorkerDeps, tmp_path: Path, storage: Any, audio: str
) -> None:
    folder = tmp_path / "ds"
    folder.mkdir()
    (tmp_path / "outside.ogg").write_bytes(ogg_opus_bytes())
    (folder / "ok.ogg").write_bytes(ogg_opus_bytes())
    meta = {"name": "voice", "license": "private", "lang": "ru"}
    rows = [
        {"external_id": "ok", "question_id": "q", "question": "Q?", "audio": "ok.ogg"},
        {"external_id": "bad", "question_id": "q", "question": "Q?", "audio": audio},
    ]
    with pytest.raises(AppError, match="аудио"):
        await do_import(deps, write_jsonl(folder / "v.jsonl", meta, rows))
    async with deps.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Dataset)) == 0
        assert await db.scalar(select(func.count()).select_from(Answer)) == 0
    assert storage.objects == {}  # загруженное раньше ошибки удалено


async def test_run_on_dataset_with_overrides(
    deps: WorkerDeps, tmp_path: Path, llm: FakeProvider, run_models: Any, replies: Any
) -> None:
    dataset_id = await mohler(deps, tmp_path)
    llm.replies = [replies.evaluation([3] * 5)]
    async with deps.sessionmaker() as db:
        run = await create_run(
            db,
            name="mohler",
            provider="ollama",
            model="small",
            run_filter=RunFilter(dataset_id=dataset_id),
            prompt_number=3,
            temperature=0.7,
            seed=7,
        )
        assert run.params == {"temperature": 0.7, "seed": 7}
        other = await create_run(
            db,
            name="все",
            provider="ollama",
            model="small",
            run_filter=RunFilter(),
            prompt_number=2,
        )
    await process_run(deps, run.id)

    async with deps.sessionmaker() as db:
        run = await get_run(db, run.id)
        evaluations = list(await db.scalars(select(Evaluation)))
    assert (run.status, run.total, run.done) == ("done", 4, 4)
    assert {e.prompt_version[:3] for e in evaluations} == {"v3#"}
    assert all(e.params["temperature"] == 0.7 and e.params["seed"] == 7 for e in evaluations)
    request = run_models.providers["small"].requests[0]
    assert (request.temperature, request.seed) == (0.7, 7)
    assert "Эталонный ответ" in request.prompt  # эталон датасета ушёл в промпт v3
    assert other.params is None


async def test_dataset_filter_excludes_user_answers(
    deps: WorkerDeps,
    tmp_path: Path,
    client: Any,
    session: dict[str, Any],
    llm: FakeProvider,
    replies: Any,
) -> None:
    question = session["questions"][0]
    await client.post(
        f"/sessions/{session['id']}/questions/{question['id']}/answer", json={"text": "ответ"}
    )
    dataset_id = await mohler(deps, tmp_path, n=2)
    llm.replies = [replies.evaluation([2] * 5)]
    async with deps.sessionmaker() as db:
        run = await create_run(
            db,
            name="r",
            provider="ollama",
            model="small",
            run_filter=RunFilter(dataset_id=dataset_id),
        )
        assert RunFilter.from_json(run.filter).dataset_id == dataset_id
    await process_run(deps, run.id)
    async with deps.sessionmaker() as db:
        assert (await get_run(db, run.id)).total == 2


async def test_export_human_scores(
    deps: WorkerDeps, tmp_path: Path, llm: FakeProvider, replies: Any
) -> None:
    dataset_id = await mohler(deps, tmp_path, n=2)
    llm.replies = [replies.evaluation([2] * 5)]
    async with deps.sessionmaker() as db:
        run = await create_run(
            db, name="r", provider="ollama", model="small", run_filter=RunFilter()
        )
    await process_run(deps, run.id)

    async with deps.sessionmaker() as db:
        rows = await select_rows(db, ExportFilter(run_ids=[run.id], dataset_id=dataset_id))
        none = await select_rows(db, ExportFilter(run_ids=[run.id], dataset_id=uuid.uuid4()))
    out = io.StringIO()
    write_csv(out, rows)
    data = sorted(csv.DictReader(io.StringIO(out.getvalue())), key=lambda r: r["dataset_item"])
    assert none == []
    assert {"human_score_max", "dataset", "dataset_item", "human_scores"} <= set(COMMON)
    assert [r["dataset_item"] for r in data] == ["E01.Q00.A0", "E01.Q01.A1"]
    assert [r["human_score"] for r in data] == ["0.0", "1.0"]
    assert {r["human_score_max"] for r in data} == {"5.0"}
    assert {r["dataset"] for r in data} == {"mohler"}
    assert data[0]["reference_answer"] == "Reference 0."
    assert json.loads(data[1]["human_scores"]) == {"grader_1": 1, "grader_2": 1, "avg": 1}


async def test_cli_dataset_commands(
    deps: WorkerDeps, tmp_path: Path, queue: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_jsonl(tmp_path / "m.jsonl", META, [text_item(i, i % 6) for i in range(6)])
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"dataset": META}) + "\n{}", encoding="utf-8")
    parser = build_parser()
    async with deps.sessionmaker() as db:
        args = parser.parse_args(["dataset", "import", str(path), "--sample", "3"])
        assert await run_command(args, deps.settings, db, queue) == 0
        args = parser.parse_args(["dataset", "import", str(bad)])
        assert await run_command(args, deps.settings, db, queue) == 1
        args = parser.parse_args(["dataset", "list"])
        assert await run_command(args, deps.settings, db, queue) == 0
        args = parser.parse_args(
            [
                "eval-run",
                "create",
                "--name",
                "r",
                "--dataset",
                "mohler-3",
                "--prompt",
                "3",
                "--temperature",
                "0.7",
                "--seed",
                "2",
            ]
        )
        assert await run_command(args, deps.settings, db, queue) == 0
        args = parser.parse_args(
            [
                "export",
                "--run",
                str(queue.runs[0]),
                "--dataset",
                "mohler-3",
                "-o",
                str(tmp_path / "x.csv"),
            ]
        )
        assert await run_command(args, deps.settings, db, queue) == 0
    out = capsys.readouterr()
    assert "mohler-3: вопросов 2, ответов 3" in out.out
    assert "mohler-3  en  CC-BY-4.0  ответов 3" in out.out
    assert "строка 2" in out.err
    async with deps.sessionmaker() as db:
        run = await db.scalar(select(EvalRun))
    assert run is not None
    assert run.params == {"temperature": 0.7, "seed": 2}
    assert run.prompt_version.startswith("v3#")
