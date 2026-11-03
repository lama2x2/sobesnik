"""Подробный разбор ответа по кнопке (010 §4): синхронно в запросе, один разбор на оценку."""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Answer, AnswerReview, Evaluation, Transcript
from sobesnik_api.errors import ConflictError
from sobesnik_api.services.evaluation import live_evaluation
from sobesnik_core.evaluation import AnswerKind
from sobesnik_core.limits import REVIEW_MAX_TOKENS
from sobesnik_core.prompt import load_prompt
from sobesnik_core.review import (
    PointStatus,
    ReviewOutput,
    make_review_repair,
    point_statuses,
    review_json_schema,
    review_prompt,
)
from sobesnik_llm import LLMProvider, LLMRequest, generate_structured

log = logging.getLogger(__name__)

REVIEW_PROMPT = "review_answer"


@dataclass(frozen=True)
class ReviewResult:
    review: AnswerReview
    evaluation: Evaluation
    statuses: list[PointStatus]
    cached: bool


def statuses_of(answer: Answer, evaluation: Evaluation) -> list[PointStatus]:
    feedback = evaluation.feedback or {}
    return point_statuses(
        answer.question.key_points,
        feedback.get("missed_points", []),
        feedback.get("wrong_points", []),
    )


async def _reviewed_text(db: AsyncSession, answer: Answer, evaluation: Evaluation) -> str:
    """Тот текст, который оценивался: у голосового — расшифровка из оценки."""
    if answer.kind != "voice":
        return answer.text or ""
    transcript = (
        await db.get(Transcript, evaluation.transcript_id) if evaluation.transcript_id else None
    )
    if transcript is None:
        raise ConflictError(
            "transcript_deleted", "расшифровка ответа удалена — разобрать его уже нельзя"
        )
    return transcript.text


async def review_answer(
    db: AsyncSession, llm: LLMProvider, answer: Answer, *, max_retries: int = 2
) -> ReviewResult:
    """Разбор последней «боевой» оценки: сохранённый — из БД, иначе вызов модели.

    Ошибки LLM пробрасываются (`502`/`503`), разбор при этом не сохраняется.
    """
    evaluation = await live_evaluation(db, answer.id)
    if evaluation is None:
        raise ConflictError("answer_not_evaluated", "ответ ещё не оценён — разбирать пока нечего")
    statuses = statuses_of(answer, evaluation)
    stored = await db.scalar(
        select(AnswerReview).where(AnswerReview.evaluation_id == evaluation.id)
    )
    if stored is not None:
        return ReviewResult(stored, evaluation, statuses, cached=True)

    text = await _reviewed_text(db, answer, evaluation)
    kind: AnswerKind = "voice" if answer.kind == "voice" else "text"
    question = answer.question
    template = load_prompt(REVIEW_PROMPT)
    rendered = review_prompt(
        template,
        question.text,
        question.key_points,
        statuses,
        text,
        (evaluation.feedback or {}).get("advice", ""),
        kind,
        question.reference_answer,
    )
    result = await generate_structured(
        llm,
        LLMRequest(
            system=rendered.system,
            prompt=rendered.user,
            temperature=template.temperature,
            seed=template.seed,
            max_tokens=REVIEW_MAX_TOKENS,
        ),
        ReviewOutput,
        json_schema=review_json_schema(),
        repair=make_review_repair(len(question.key_points), text),
        max_retries=max_retries,
    )
    output = result.value
    attempts = result.attempts
    last = attempts[-1]
    known_in = [a.tokens_in for a in attempts if a.tokens_in is not None]
    known_out = [a.tokens_out for a in attempts if a.tokens_out is not None]
    # два нажатия подряд: второй разбор не записывается, отдаётся первый
    await db.execute(
        insert(AnswerReview)
        .values(
            answer_id=answer.id,
            evaluation_id=evaluation.id,
            provider=last.provider,
            model=last.model,
            prompt_version=template.version,
            content={
                "reference": output.reference,
                "points": [p.model_dump() for p in output.points],
                "mistakes": [m.model_dump() for m in output.mistakes],
                "question_issue": output.question_issue or None,
            },
            raw_response=last.text,
            attempts=len(attempts),
            latency_ms=sum(a.latency_ms for a in attempts),
            tokens_in=sum(known_in) if known_in else None,
            tokens_out=sum(known_out) if known_out else None,
        )
        .on_conflict_do_nothing(index_elements=["evaluation_id"])
    )
    await db.commit()
    review = await db.scalar(
        select(AnswerReview).where(AnswerReview.evaluation_id == evaluation.id)
    )
    assert review is not None
    log.info(
        "answer reviewed",
        extra={
            "answer_id": str(answer.id),
            "evaluation_id": str(evaluation.id),
            "attempts": review.attempts,
            "llm_model": review.model,
            "latency_ms": review.latency_ms,
            "mistakes": len(output.mistakes),
            "question_issue": bool(output.question_issue),
        },
    )
    return ReviewResult(review, evaluation, statuses, cached=False)
