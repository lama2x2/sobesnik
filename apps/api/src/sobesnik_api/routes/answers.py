import uuid

from fastapi import APIRouter

from sobesnik_api.db.models import Answer, Evaluation
from sobesnik_api.deps import DB, Queue
from sobesnik_api.schemas import (
    AnswerAccepted,
    AnswerCreate,
    AnswerOut,
    CriterionScoreOut,
    EvaluationOut,
)
from sobesnik_api.services.answers import get_answer, submit_answer
from sobesnik_api.services.evaluation import live_evaluation
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
        rubric_version=evaluation.rubric_version,
        prompt_version=evaluation.prompt_version,
        model=evaluation.model,
        latency_ms=evaluation.latency_ms,
        created_at=evaluation.created_at,
    )


def answer_out(answer: Answer, evaluation: Evaluation | None) -> AnswerOut:
    return AnswerOut(
        id=answer.id,
        session_id=answer.session_id,
        question_id=answer.question_id,
        kind=answer.kind,
        text=answer.text,
        status=answer.status,
        error=answer.error,
        created_at=answer.created_at,
        evaluation=evaluation_out(evaluation) if evaluation else None,
    )


@router.post("/sessions/{session_id}/questions/{question_id}/answer", status_code=202)
async def post_answer(
    session_id: uuid.UUID, question_id: uuid.UUID, body: AnswerCreate, db: DB, queue: Queue
) -> AnswerAccepted:
    answer = await submit_answer(db, queue, session_id, question_id, body.text)
    return AnswerAccepted(answer_id=answer.id, status=answer.status)


@router.get("/answers/{answer_id}")
async def read_answer(answer_id: uuid.UUID, db: DB) -> AnswerOut:
    answer = await get_answer(db, answer_id)
    return answer_out(answer, await live_evaluation(db, answer.id))
