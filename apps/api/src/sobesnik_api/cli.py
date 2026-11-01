"""CLI исследователя (003 §9, 004 §9): прогоны оценки, ручные расшифровки и выгрузка CSV.

    docker compose run --rm tools uv run sobesnik-cli eval-run create --name s --model qwen3:1.7b
    docker compose run --rm tools uv run sobesnik-cli transcript add ANSWER_ID --file ref.txt
    docker compose run --rm tools uv run sobesnik-cli export --run RUN_ID -o research/data/x.csv

Нужны Postgres и Redis; LLM не нужна — оценивает воркер.
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
from sobesnik_api.services.eval_runs import RunFilter, create_run, get_run, run_config
from sobesnik_api.services.voice import add_reference_transcript
from sobesnik_api.settings import Settings

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
    create.add_argument("--rubric", type=int, help="номер версии рубрики; по умолчанию последняя")
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

    export = commands.add_parser("export", help="выгрузить оценки в CSV")
    export.add_argument("--run", type=uuid.UUID, action="append", default=[], dest="runs")
    export.add_argument("--live", action="store_true", help="«боевые» оценки (вне прогонов)")
    export.add_argument("--session", type=uuid.UUID)
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


async def run_command(
    args: argparse.Namespace, settings: Settings, db: AsyncSession, queue: TaskQueue
) -> int:
    if args.command == "export":
        if not args.runs and not args.live:
            print("укажите --run и/или --live", file=sys.stderr)
            return 2
        rows = await select_rows(
            db, ExportFilter(run_ids=args.runs, live=args.live, session_id=args.session)
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8-sig", newline="") as out:
            count = write_csv(out, rows, args.format)
        print(f"{args.output}: {count} строк, оценок {len(rows)}")
        return 0

    if args.command == "transcript":
        text = args.file.read_text(encoding="utf-8")
        added = await add_reference_transcript(db, args.answer_id, text, args.language)
        print(f"{added.id}  reference  {len(added.text)} символов")
        return 0

    if args.action == "create":
        eval_config = settings.llm_config("eval")
        run = await create_run(
            db,
            name=args.name,
            provider=eval_config.provider,
            model=args.model or eval_config.model,
            run_filter=RunFilter(args.session, args.user, args.since, args.until),
            rubric_number=args.rubric,
            transcript_kind=args.transcript,
        )
        await queue.enqueue_run(run.id)
        print(format_run(run))
    elif args.action == "status":
        print(format_run(await get_run(db, args.run_id)))
        return 0
    else:  # restart
        run = await get_run(db, args.run_id)
        run_config(run, max_retries=0, num_ctx=None)  # рубрика и промпт не изменились
        if run.status == "running":
            print("прогон уже идёт", file=sys.stderr)
            return 1
        run.status, run.error = "queued", None
        await db.commit()
        await queue.enqueue_run(run.id)
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
