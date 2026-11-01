"""Импорт открытых датасетов из единого JSONL (EVAL-8, 005 §6).

Ответы датасета — обычные `answer` с `source = dataset` и без пользователя: их оценивают
прогоны тем же кодом, что и ответы пользователей. «Боевой» оценки у них нет.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Answer, Audio, Dataset, DatasetItem, Question, Transcript
from sobesnik_api.errors import (
    STORAGE_UNAVAILABLE,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    UnprocessableError,
)
from sobesnik_api.queue import TaskQueue
from sobesnik_api.services.voice import AudioLimits, audio_key, check_audio
from sobesnik_asr import AudioStorage, StorageError
from sobesnik_core.dataset import ParsedDataset, stratified_sample

log = logging.getLogger(__name__)

DATASET_GEN_MODEL = "dataset"
"""`question.gen_model` у вопросов датасета: их не генерировала модель."""


@dataclass(frozen=True)
class ImportResult:
    dataset: Dataset
    questions: int
    answers: int
    voice: int


def _audio_path(base_dir: Path, relative: str) -> Path:
    """Аудио — только внутри папки файла JSONL."""
    base = base_dir.resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise UnprocessableError("bad_audio_path", f"аудио вне папки датасета: {relative}")
    if not path.is_file():
        raise UnprocessableError("bad_audio_path", f"нет файла аудио: {relative}")
    return path


async def import_dataset(
    db: AsyncSession,
    queue: TaskQueue,
    storage: AudioStorage,
    parsed: ParsedDataset,
    *,
    base_dir: Path,
    limits: AudioLimits,
    name: str | None = None,
    sample: int | None = None,
    seed: int = 1,
) -> ImportResult:
    """Одна транзакция: датасет, вопросы, ответы. Голосовые ответы ставятся в распознавание.

    Датасет с таким именем уже есть — `409`. Ошибка в аудио — ничего не записано.
    """
    meta = parsed.meta
    items = parsed.items
    if sample is not None:
        items = stratified_sample(items, sample, meta.score_key, seed=seed)
    name = name or (f"{meta.name}-{len(items)}" if sample is not None else meta.name)
    if await db.scalar(select(Dataset.id).where(Dataset.name == name)) is not None:
        raise ConflictError("dataset_exists", f"датасет {name} уже импортирован")

    dataset = Dataset(
        id=uuid.uuid4(),
        name=name,
        source_url=meta.source_url,
        license=meta.license,
        lang=meta.lang,
        score_key=meta.score_key,
        score_min=meta.score_min,
        score_max=meta.score_max,
        synthetic=meta.synthetic,
        note=meta.note,
    )
    db.add(dataset)
    questions: dict[str, Question] = {}
    voice_ids: list[uuid.UUID] = []
    uploaded: list[str] = []
    try:
        for item in items:
            question = questions.get(item.question_id)
            if question is None:
                question = Question(
                    id=uuid.uuid4(),
                    text=item.question,
                    key_points=item.key_points,
                    reference_answer=item.reference_answer,
                    source="dataset",
                    dataset_id=dataset.id,
                    external_id=item.question_id,
                    gen_model=DATASET_GEN_MODEL,
                    prompt_version="",
                )
                questions[item.question_id] = question
                db.add(question)
            row = DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                external_id=item.external_id,
                human_scores=item.human_scores,
            )
            db.add(row)
            answer = Answer(
                id=uuid.uuid4(),
                question_id=question.id,
                source="dataset",
                dataset_item_id=row.id,
                kind="text" if item.answer is not None else "voice",
                text=item.answer.strip() if item.answer is not None else None,
                status="imported" if item.answer is not None else "received",
            )
            db.add(answer)
            if item.audio is not None:
                data = _audio_path(base_dir, item.audio).read_bytes()
                info = check_audio(data, limits)
                key = audio_key(answer.id)
                try:
                    await storage.put(key, data, info.mime)
                except StorageError as exc:
                    raise ServiceUnavailableError(
                        "storage_unavailable", STORAGE_UNAVAILABLE
                    ) from exc
                uploaded.append(key)
                await db.flush()  # строка answer — раньше audio, которое на неё ссылается
                db.add(
                    Audio(
                        answer_id=answer.id,
                        s3_key=key,
                        mime=info.mime,
                        size_bytes=len(data),
                        duration_s=info.duration_s,
                        expires_at=datetime.now(UTC) + timedelta(days=limits.ttl_days),
                    )
                )
                voice_ids.append(answer.id)
        await db.commit()
    except BaseException:
        await db.rollback()
        for key in uploaded:
            try:
                await storage.delete(key)
            except StorageError:
                log.warning("orphan audio left in storage", extra={"s3_key": key})
        raise

    for answer_id in voice_ids:
        try:
            await queue.enqueue_transcription(answer_id)
        except Exception as exc:  # ответ в received: его подхватит воркер при старте
            log.warning(
                "enqueue failed", extra={"answer_id": str(answer_id), "error": type(exc).__name__}
            )
    log.info(
        "dataset imported",
        extra={"dataset": name, "answers": len(items), "voice": len(voice_ids)},
    )
    return ImportResult(dataset, len(questions), len(items), len(voice_ids))


@dataclass(frozen=True)
class DatasetSummary:
    dataset: Dataset
    answers: int
    voice: int
    transcribed: int


async def list_datasets(db: AsyncSession) -> list[DatasetSummary]:
    datasets = list(await db.scalars(select(Dataset).order_by(Dataset.imported_at)))
    summaries = []
    for dataset in datasets:
        of_dataset = (
            select(func.count())
            .select_from(Answer)
            .join(DatasetItem, DatasetItem.id == Answer.dataset_item_id)
            .where(DatasetItem.dataset_id == dataset.id)
        )
        answers = await db.scalar(of_dataset)
        voice = await db.scalar(of_dataset.where(Answer.kind == "voice"))
        transcribed = await db.scalar(
            of_dataset.where(
                Answer.kind == "voice",
                select(Transcript.id)
                .join(Audio, Audio.id == Transcript.audio_id)
                .where(Audio.answer_id == Answer.id, Transcript.kind == "asr")
                .exists(),
            )
        )
        summaries.append(DatasetSummary(dataset, answers or 0, voice or 0, transcribed or 0))
    return summaries


async def get_dataset(db: AsyncSession, name: str) -> Dataset:
    dataset = await db.scalar(select(Dataset).where(Dataset.name == name))
    if dataset is None:
        raise NotFoundError("dataset_not_found", f"датасет {name} не найден")
    return dataset
