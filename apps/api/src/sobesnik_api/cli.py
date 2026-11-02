"""CLI исследователя (003 §9, 004 §9, 005 §6): датасеты, прогоны, расшифровки и выгрузка CSV.

    docker compose run --rm tools uv run sobesnik-cli dataset import FILE.jsonl --sample 30
    docker compose run --rm tools uv run sobesnik-cli eval-run create --name s --model qwen3:1.7b
    docker compose run --rm tools uv run sobesnik-cli transcript add ANSWER_ID --file ref.txt
    docker compose run --rm tools uv run sobesnik-cli export --run RUN_ID -o research/data/x.csv

Нужны Postgres и Redis (импорт голоса — ещё хранилище); LLM не нужна — оценивает воркер.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import EvalRun
from sobesnik_api.db.session import make_engine, make_sessionmaker
from sobesnik_api.errors import AppError
from sobesnik_api.export import ExportFilter, select_rows, write_csv
from sobesnik_api.queue import ArqQueue, TaskQueue
from sobesnik_api.services.datasets import get_dataset, import_dataset, list_datasets
from sobesnik_api.services.eval_runs import RunFilter, create_run, get_run, restart_run
from sobesnik_api.services.voice import AudioLimits, add_reference_transcript
from sobesnik_api.settings import Settings
from sobesnik_core.dataset import DatasetFormatError, parse_jsonl

POLL_S = 5.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sobesnik-cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("eval-run", help="прогоны оценки").add_subparsers(
        dest="action", required=True
    )
    create = run.add_parser("create", help="создать прогон и поставить в очередь")
    create.add_argument("--name", required=True)
    create.add_argument("--model", help="модель; по умолчанию — модель роли eval")
    create.add_argument("--session", type=uuid.UUID)
    create.add_argument("--user", type=uuid.UUID)
    create.add_argument("--since", type=date.fromisoformat, help="YYYY-MM-DD")
    create.add_argument("--until", type=date.fromisoformat, help="YYYY-MM-DD, включительно")
    create.add_argument("--dataset", help="только ответы этого датасета (имя)")
    create.add_argument("--rubric", type=int, help="номер версии рубрики; по умолчанию последняя")
    create.add_argument("--prompt", type=int, help="номер версии промпта; по умолчанию последняя")
    create.add_argument("--temperature", type=float, help="вместо значения из промпта")
    create.add_argument("--seed", type=int, help="вместо значения из промпта")
    create.add_argument(
        "--transcript",
        choices=["asr", "reference"],
        help="только голосовые ответы с таким транскриптом; без флага — как в бою",
    )
    create.add_argument("--wait", action="store_true", help="ждать окончания и печатать прогресс")
    status = run.add_parser("status", help="статус прогона")
    status.add_argument("run_id", type=uuid.UUID)
    restart = run.add_parser("restart", help="перезапустить: готовые оценки пропускаются")
    restart.add_argument("run_id", type=uuid.UUID)
    restart.add_argument("--wait", action="store_true")

    transcript = commands.add_parser("transcript", help="транскрипты голосовых ответов")
    transcript_actions = transcript.add_subparsers(dest="action", required=True)
    add = transcript_actions.add_parser("add", help="добавить ручную расшифровку (reference)")
    add.add_argument("answer_id", type=uuid.UUID)
    add.add_argument("--file", type=Path, required=True, help="текст расшифровки, UTF-8")
    add.add_argument("--language", default="ru")
    add.add_argument("--from-model", help="расшифровка сделана этой моделью, а не человеком")

    dataset = commands.add_parser("dataset", help="открытые датасеты").add_subparsers(
        dest="action", required=True
    )
    imp = dataset.add_parser("import", help="импорт из единого JSONL")
    imp.add_argument("file", type=Path)
    imp.add_argument("--name", help="имя в БД; по умолчанию из заголовка (+ размер выборки)")
    imp.add_argument("--sample", type=int, help="стратифицированная выборка N ответов")
    imp.add_argument("--seed", type=int, default=1)
    dataset.add_parser("list", help="импортированные датасеты")

    export = commands.add_parser("export", help="выгрузить оценки в CSV")
    export.add_argument("--run", type=uuid.UUID, action="append", default=[], dest="runs")
    export.add_argument("--live", action="store_true", help="«боевые» оценки (вне прогонов)")
    export.add_argument("--session", type=uuid.UUID)
    export.add_argument("--dataset", help="только ответы этого датасета (имя)")
    export.add_argument("--format", choices=["wide", "long"], default="wide")
    export.add_argument("-o", "--output", type=Path, required=True)
    return parser


def format_run(run: EvalRun) -> str:
    line = (
        f"{run.id}  {run.name}  {run.status}  модель {run.model}  "
        f"готово {run.done}/{run.total}, сбоев {run.failed}"
    )
    return f"{line}\n  ошибка: {run.error}" if run.error else line


async def _wait(db: AsyncSession, run_id: uuid.UUID) -> EvalRun:
    while True:
        db.expire_all()
        run = await get_run(db, run_id)
        print(format_run(run), flush=True)
        if run.status in ("done", "failed"):
            return run
        await asyncio.sleep(POLL_S)


async def _dataset_command(
    args: argparse.Namespace, settings: Settings, db: AsyncSession, queue: TaskQueue
) -> int:
    if args.action == "list":
        for s in await list_datasets(db):
            d = s.dataset
            voice = f", голос {s.transcribed}/{s.voice} распознано" if s.voice else ""
            print(f"{d.name}  {d.lang}  {d.license}  ответов {s.answers}{voice}")
        return 0
    try:
        with args.file.open(encoding="utf-8") as f:
            parsed = parse_jsonl(f)
    except DatasetFormatError as exc:
        print(f"ошибка: {args.file}: {exc}", file=sys.stderr)
        return 1
    limits = AudioLimits(
        settings.audio_max_bytes, settings.audio_max_seconds, settings.audio_ttl_days
    )
    result = await import_dataset(
        db,
        queue,
        settings.audio_storage(),
        parsed,
        base_dir=args.file.parent,
        limits=limits,
        name=args.name,
        sample=args.sample,
        seed=args.seed,
    )
    print(
        f"{result.dataset.name}: вопросов {result.questions}, ответов {result.answers}"
        + (f", голосовых {result.voice} — в распознавании" if result.voice else "")
    )
    return 0


async def run_command(
    args: argparse.Namespace, settings: Settings, db: AsyncSession, queue: TaskQueue
) -> int:
    if args.command == "export":
        if not args.runs and not args.live:
            print("укажите --run и/или --live", file=sys.stderr)
            return 2
        dataset_id = (await get_dataset(db, args.dataset)).id if args.dataset else None
        rows = await select_rows(
            db,
            ExportFilter(
                run_ids=args.runs, live=args.live, session_id=args.session, dataset_id=dataset_id
            ),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8-sig", newline="") as out:
            count = write_csv(out, rows, args.format)
        print(f"{args.output}: {count} строк, оценок {len(rows)}")
        return 0

    if args.command == "transcript":
        text = args.file.read_text(encoding="utf-8")
        params = {"from_model": args.from_model} if args.from_model else None
        added = await add_reference_transcript(db, args.answer_id, text, args.language, params)
        print(f"{added.id}  reference  {len(added.text)} символов")
        return 0

    if args.command == "dataset":
        return await _dataset_command(args, settings, db, queue)

    if args.action == "create":
        eval_config = settings.llm_config("eval")
        run = await create_run(
            db,
            name=args.name,
            provider=eval_config.provider,
            model=args.model or eval_config.model,
            run_filter=RunFilter(
                args.session,
                args.user,
                args.since,
                args.until,
                (await get_dataset(db, args.dataset)).id if args.dataset else None,
            ),
            rubric_number=args.rubric,
            prompt_number=args.prompt,
            transcript_kind=args.transcript,
            temperature=args.temperature,
            seed=args.seed,
        )
        await queue.enqueue_run(run.id)
        print(format_run(run))
    elif args.action == "status":
        print(format_run(await get_run(db, args.run_id)))
        return 0
    else:  # restart
        run = await restart_run(db, queue, args.run_id)
        print(format_run(run))
    if args.wait:
        run = await _wait(db, run.id)
        return 0 if run.status == "done" else 1
    return 0


async def amain(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    engine = make_engine(settings.database_url)
    queue = ArqQueue(settings.redis_url)
    try:
        async with make_sessionmaker(engine)() as db:
            return await run_command(args, settings, db, queue)
    except AppError as exc:
        print(f"ошибка: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await queue.aclose()
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(amain()))
