import uuid

from fastapi import APIRouter, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Answer, Audio, Evaluation, Transcript
from sobesnik_api.deps import DB, AppSettings, Queue, Storage
from sobesnik_api.errors import PayloadTooLargeError
from sobesnik_api.schemas import (
    AnswerAccepted,
    AnswerCreate,
    AnswerOut,
    AudioOut,
    CriterionScoreOut,
    EvaluationOut,
    TopicDocOut,
    TranscriptOut,
)
from sobesnik_api.services.answers import get_answer, submit_answer
from sobesnik_api.services.evaluation import live_evaluation
from sobesnik_api.services.voice import (
    AudioLimits,
    answer_audio,
    latest_transcript,
    submit_voice_answer,
)
from sobesnik_core.rubric import Rubric, load_rubric
from sobesnik_core.topics import topic_docs
from sobesnik_core.versioned import version_number

router = APIRouter(tags=["answers"])


def _rubric_of(evaluation: Evaluation) -> Rubric | None:
    """Рубрика, по которой сделана оценка: названия и веса критериев для ответа API."""
    try:
        return load_rubric(version_number(evaluation.rubric_version))
    except (ValueError, FileNotFoundError):
        return None


def evaluation_out(evaluation: Evaluation) -> EvaluationOut:
    assert evaluation.scores is not None and evaluation.total is not None
    assert evaluation.passed is not None and evaluation.feedback is not None
    rubric = _rubric_of(evaluation)
    criteria = {c.id: c for c in rubric.criteria} if rubric else {}
    # JSONB не хранит порядок ключей: критерии идут в порядке рубрики
    order = [cid for cid in criteria if cid in evaluation.scores]
    order += sorted(set(evaluation.scores) - set(order))
    scores = []
    for cid in order:
        item = evaluation.scores[cid]
        criterion = criteria.get(cid)
        scores.append(
            CriterionScoreOut(
                criterion=cid,
                title=criterion.title if criterion else cid,
                weight=criterion.weight if criterion else 0.0,
                score=item["score"],
                reason=item["reason"],
            )
        )
    return EvaluationOut(
        id=evaluation.id,
        total=evaluation.total,
        passed=evaluation.passed,
        scores=scores,
        missed_points=evaluation.feedback.get("missed_points", []),
        advice=evaluation.feedback.get("advice", ""),
        asr_doubts=evaluation.feedback.get("asr_doubts", []),
        rubric_version=evaluation.rubric_version,
        prompt_version=evaluation.prompt_version,
        model=evaluation.model,
        latency_ms=evaluation.latency_ms,
        created_at=evaluation.created_at,
    )


def audio_out(audio: Audio | None) -> AudioOut | None:
    if audio is None:
        return None
    return AudioOut(
        mime=audio.mime,
        duration_s=audio.duration_s,
        expires_at=audio.expires_at,
        deleted=audio.deleted_at is not None,
    )


def transcript_out(transcript: Transcript | None) -> TranscriptOut | None:
    if transcript is None:
        return None
    return TranscriptOut(
        id=transcript.id,
        kind=transcript.kind,
        text=transcript.text,
        language=transcript.language,
        asr_model=transcript.asr_model,
        latency_ms=transcript.latency_ms,
    )


ANSWER_DOCS = 2
"""Под оценкой — не больше двух ссылок, одной строкой (007 §8.4)."""


def docs_out(topic: str | None, limit: int | None = None) -> list[TopicDocOut]:
    return [TopicDocOut(title=d.title, url=d.url) for d in topic_docs(topic)[:limit]]


async def answer_out(db: AsyncSession, answer: Answer, evaluation: Evaluation | None) -> AnswerOut:
    """Транскрипт — тот, что оценён «боевой» оценкой, а пока её нет — последний распознанный."""
    audio = transcript = None
    if answer.kind == "voice":
        audio = await answer_audio(db, answer.id)
        if evaluation is not None and evaluation.transcript_id is not None:
            transcript = await db.get(Transcript, evaluation.transcript_id)
        if transcript is None:
            transcript = await latest_transcript(db, answer.id)
    return AnswerOut(
        id=answer.id,
        session_id=answer.session_id,
        question_id=answer.question_id,
        kind=answer.kind,
        text=answer.text,
        status=answer.status,
        error=answer.error,
        created_at=answer.created_at,
        audio=audio_out(audio),
        transcript=transcript_out(transcript),
        evaluation=evaluation_out(evaluation) if evaluation else None,
        topic=answer.question.topic_slug,
        docs=docs_out(answer.question.topic_slug, ANSWER_DOCS),
    )


@router.post("/sessions/{session_id}/questions/{question_id}/answer", status_code=202)
async def post_answer(
    session_id: uuid.UUID, question_id: uuid.UUID, body: AnswerCreate, db: DB, queue: Queue
) -> AnswerAccepted:
    answer = await submit_answer(db, queue, session_id, question_id, body.text)
    return AnswerAccepted(answer_id=answer.id, status=answer.status)


async def _read_limited(file: UploadFile, max_bytes: int) -> bytes:
    """Читает файл кусками и бросает 413, как только он превысил лимит."""
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise PayloadTooLargeError(
                "audio_too_large", f"файл больше {max_bytes // (1024 * 1024)} МБ"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/sessions/{session_id}/questions/{question_id}/answer/voice", status_code=202)
async def post_voice_answer(
    session_id: uuid.UUID,
    question_id: uuid.UUID,
    file: UploadFile,
    db: DB,
    queue: Queue,
    storage: Storage,
    settings: AppSettings,
) -> AnswerAccepted:
    limits = AudioLimits(
        max_bytes=settings.audio_max_bytes,
        max_seconds=settings.audio_max_seconds,
        ttl_days=settings.audio_ttl_days,
    )
    data = await _read_limited(file, limits.max_bytes)
    answer = await submit_voice_answer(db, queue, storage, session_id, question_id, data, limits)
    return AnswerAccepted(answer_id=answer.id, status=answer.status)


@router.get("/answers/{answer_id}")
async def read_answer(answer_id: uuid.UUID, db: DB) -> AnswerOut:
    answer = await get_answer(db, answer_id)
    return await answer_out(db, answer, await live_evaluation(db, answer.id))
