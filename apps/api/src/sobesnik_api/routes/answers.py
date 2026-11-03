import uuid

from fastapi import APIRouter, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.asr_status import ASRStatus, asr_ready
from sobesnik_api.auth import Caller, CallerDep
from sobesnik_api.db.models import Answer, AnswerReview, Audio, Evaluation, Transcript
from sobesnik_api.deps import DB, LLM, AppSettings, ASRStatusDep, Queue, Storage
from sobesnik_api.errors import NotFoundError, PayloadTooLargeError
from sobesnik_api.routes.sessions import owned_session
from sobesnik_api.schemas import (
    DOCS_SHOWN,
    AnswerAccepted,
    AnswerCreate,
    AnswerOut,
    AudioOut,
    CriterionScoreOut,
    EvaluationOut,
    ReviewMistakeOut,
    ReviewOut,
    ReviewPointOut,
    TranscriptOut,
    docs_out,
)
from sobesnik_api.services.answers import get_answer, submit_answer
from sobesnik_api.services.evaluation import live_evaluation
from sobesnik_api.services.review import ReviewResult, review_answer, statuses_of
from sobesnik_api.services.voice import (
    AudioLimits,
    answer_audio,
    latest_transcript,
    submit_voice_answer,
)
from sobesnik_core.asr_doubts import filter_doubts
from sobesnik_core.rubric import Rubric, load_rubric
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
        # шумные сомнения убираются при выдаче, в оценке и CSV остаётся всё (010 §6.3)
        asr_doubts=filter_doubts(evaluation.feedback.get("asr_doubts", [])),
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


WAITING_ASR = ("received", "transcribing")


async def answer_out(
    db: AsyncSession,
    answer: Answer,
    evaluation: Evaluation | None,
    asr: tuple[ASRStatus, str] | None = None,
) -> AnswerOut:
    """Транскрипт — тот, что оценён «боевой» оценкой, а пока её нет — последний распознанный.

    `asr` — статус и модель распознавания: голосовому, которое ждёт распознавания, отдаётся
    `asr_ready` (011 §4)."""
    audio = transcript = None
    ready = None
    if answer.kind == "voice" and answer.status in WAITING_ASR and asr is not None:
        ready = await asr_ready(*asr)
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
        docs=docs_out(answer.question.topic_slug, DOCS_SHOWN),
        has_review=evaluation is not None
        and await db.scalar(
            select(AnswerReview.id).where(AnswerReview.evaluation_id == evaluation.id)
        )
        is not None,
        asr_ready=ready,
    )


@router.post(
    "/sessions/{session_id}/questions/{question_id}/answer",
    status_code=202,
    response_model_exclude_none=True,
)
async def post_answer(
    session_id: uuid.UUID,
    question_id: uuid.UUID,
    body: AnswerCreate,
    db: DB,
    queue: Queue,
    caller: CallerDep,
) -> AnswerAccepted:
    await owned_session(db, caller, session_id)
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
    asr_status: ASRStatusDep,
    caller: CallerDep,
) -> AnswerAccepted:
    await owned_session(db, caller, session_id)
    limits = AudioLimits(
        max_bytes=settings.audio_max_bytes,
        max_seconds=settings.audio_max_seconds,
        ttl_days=settings.audio_ttl_days,
    )
    data = await _read_limited(file, limits.max_bytes)
    answer = await submit_voice_answer(db, queue, storage, session_id, question_id, data, limits)
    return AnswerAccepted(
        answer_id=answer.id,
        status=answer.status,
        asr_ready=await asr_ready(asr_status, settings.asr_model),
    )


async def owned_answer(db: AsyncSession, caller: Caller, answer_id: uuid.UUID) -> Answer:
    """Свой ответ; чужой — `404`, как несуществующий (009 §5.1)."""
    answer = await get_answer(db, answer_id)
    if not caller.owns(answer.user_id):
        raise NotFoundError("answer_not_found", "ответ не найден")
    return answer


@router.get("/answers/{answer_id}")
async def read_answer(
    answer_id: uuid.UUID,
    db: DB,
    settings: AppSettings,
    asr_status: ASRStatusDep,
    caller: CallerDep,
) -> AnswerOut:
    answer = await owned_answer(db, caller, answer_id)
    evaluation = await live_evaluation(db, answer.id)
    return await answer_out(db, answer, evaluation, (asr_status, settings.asr_model))


def review_out(answer: Answer, result: ReviewResult) -> ReviewOut:
    content = result.review.content
    comments = {p["n"]: p.get("comment", "") for p in content.get("points", [])}
    key_points = answer.question.key_points
    return ReviewOut(
        answer_id=answer.id,
        evaluation_id=result.evaluation.id,
        cached=result.cached,
        reference=content["reference"],
        points=[
            ReviewPointOut(n=n, text=text, status=status, comment=comments.get(n, ""))
            for n, (text, status) in enumerate(zip(key_points, result.statuses, strict=True), 1)
        ],
        mistakes=[ReviewMistakeOut(**m) for m in content.get("mistakes", [])],
        question_issue=content.get("question_issue"),
        docs=docs_out(answer.question.topic_slug),
        model=result.review.model,
        latency_ms=result.review.latency_ms,
    )


def _user_answer(answer: Answer) -> Answer:
    """Разбираются только ответы пользователей: у ответа датасета хозяина нет (010 §4.4)."""
    if answer.source != "user":
        raise NotFoundError("answer_not_found", "ответ не найден")
    return answer


@router.post("/answers/{answer_id}/review")
async def post_review(
    answer_id: uuid.UUID, db: DB, llm: LLM, settings: AppSettings, caller: CallerDep
) -> ReviewOut:
    """Подробный разбор: готовый — из БД, иначе вызов модели (~20–40 с на Маке)."""
    answer = _user_answer(await owned_answer(db, caller, answer_id))
    result = await review_answer(db, llm, answer, max_retries=settings.llm_max_retries)
    return review_out(answer, result)


@router.get("/answers/{answer_id}/review")
async def read_review(answer_id: uuid.UUID, db: DB, caller: CallerDep) -> ReviewOut:
    """Готовый разбор последней оценки; нет — `404 review_not_found`, модель не вызывается."""
    answer = _user_answer(await owned_answer(db, caller, answer_id))
    evaluation = await live_evaluation(db, answer.id)
    review = (
        await db.scalar(select(AnswerReview).where(AnswerReview.evaluation_id == evaluation.id))
        if evaluation
        else None
    )
    if evaluation is None or review is None:
        raise NotFoundError("review_not_found", "разбора ещё нет")
    return review_out(
        answer, ReviewResult(review, evaluation, statuses_of(answer, evaluation), cached=True)
    )
