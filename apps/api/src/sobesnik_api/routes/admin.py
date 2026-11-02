"""Служебные эндпоинты исследователя (006 §5.2): датасеты, прогоны, выгрузка CSV.

Те же функции, что у `sobesnik-cli`; доступ — по админ-токену (`auth.require_admin_token`).
"""

import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Query, UploadFile
from fastapi.responses import Response

from sobesnik_api.auth import require_admin_token
from sobesnik_api.deps import DB, AppSettings, Queue, Storage
from sobesnik_api.errors import PayloadTooLargeError, UnprocessableError
from sobesnik_api.export import ExportFilter, select_rows, write_csv
from sobesnik_api.schemas import DatasetImportOut, DatasetOut, EvalRunCreate, EvalRunOut
from sobesnik_api.services.datasets import get_dataset, import_dataset, list_datasets
from sobesnik_api.services.eval_runs import RunFilter, create_run, get_run, restart_run
from sobesnik_api.services.voice import AudioLimits
from sobesnik_core.dataset import DatasetFormatError, parse_jsonl

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_token)])

MAX_JSONL_BYTES = 20 * 1024 * 1024


@router.get("/datasets")
async def read_datasets(db: DB) -> list[DatasetOut]:
    return [
        DatasetOut(
            name=s.dataset.name,
            lang=s.dataset.lang,
            license=s.dataset.license,
            source_url=s.dataset.source_url,
            answers=s.answers,
            voice=s.voice,
            transcribed=s.transcribed,
            imported_at=s.dataset.imported_at,
        )
        for s in await list_datasets(db)
    ]


@router.post("/datasets", status_code=201)
async def post_dataset(
    file: UploadFile,
    db: DB,
    queue: Queue,
    storage: Storage,
    settings: AppSettings,
    name: Annotated[str | None, Form()] = None,
    sample: Annotated[int | None, Form(ge=1)] = None,
    seed: Annotated[int, Form()] = 1,
) -> DatasetImportOut:
    """Импорт текстового датасета из JSONL. Голосовой — только CLI: аудио лежит на диске рядом."""
    data = await file.read(MAX_JSONL_BYTES + 1)
    if len(data) > MAX_JSONL_BYTES:
        raise PayloadTooLargeError("file_too_large", "файл больше 20 МБ")
    try:
        parsed = parse_jsonl(io.StringIO(data.decode("utf-8-sig")))
    except (UnicodeDecodeError, DatasetFormatError) as exc:
        raise UnprocessableError("bad_dataset", f"ошибка в JSONL: {exc}") from exc
    if any(item.audio is not None for item in parsed.items):
        raise UnprocessableError(
            "voice_dataset_cli_only", "голосовой датасет импортируется через sobesnik-cli"
        )
    limits = AudioLimits(
        settings.audio_max_bytes, settings.audio_max_seconds, settings.audio_ttl_days
    )
    result = await import_dataset(
        db,
        queue,
        storage,
        parsed,
        base_dir=Path("."),  # аудио в датасете нет: папка не читается
        limits=limits,
        name=name,
        sample=sample,
        seed=seed,
    )
    return DatasetImportOut(
        dataset=result.dataset.name, questions=result.questions, answers=result.answers
    )


@router.post("/eval-runs", status_code=202)
async def post_eval_run(
    body: EvalRunCreate, db: DB, queue: Queue, settings: AppSettings
) -> EvalRunOut:
    eval_config = settings.llm_config("eval")
    dataset_id = (await get_dataset(db, body.dataset)).id if body.dataset else None
    run = await create_run(
        db,
        name=body.name,
        provider=eval_config.provider,
        model=body.model or eval_config.model,
        run_filter=RunFilter(body.session_id, body.user_id, body.since, body.until, dataset_id),
        rubric_number=body.rubric,
        prompt_number=body.prompt,
        transcript_kind=body.transcript,
        temperature=body.temperature,
        seed=body.seed,
    )
    await queue.enqueue_run(run.id)
    return EvalRunOut.model_validate(run)


@router.get("/eval-runs/{run_id}")
async def read_eval_run(run_id: uuid.UUID, db: DB) -> EvalRunOut:
    return EvalRunOut.model_validate(await get_run(db, run_id))


@router.post("/eval-runs/{run_id}/restart", status_code=202)
async def post_restart(run_id: uuid.UUID, db: DB, queue: Queue) -> EvalRunOut:
    return EvalRunOut.model_validate(await restart_run(db, queue, run_id))


@router.get("/evaluations.csv")
async def read_evaluations_csv(
    db: DB,
    run: Annotated[list[uuid.UUID], Query()] = [],  # noqa: B006 — так FastAPI читает повторы
    live: bool = False,
    session: uuid.UUID | None = None,
    dataset: str | None = None,
    format: Literal["wide", "long"] = "wide",
) -> Response:
    if not run and not live:
        raise UnprocessableError("no_filter", "укажите run и/или live=true")
    dataset_id = (await get_dataset(db, dataset)).id if dataset else None
    rows = await select_rows(
        db, ExportFilter(run_ids=run, live=live, session_id=session, dataset_id=dataset_id)
    )
    out = io.StringIO()
    write_csv(out, rows, format)
    name = f"evaluations-{datetime.now(UTC):%Y%m%d-%H%M%S}.csv"
    return Response(
        content=out.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
