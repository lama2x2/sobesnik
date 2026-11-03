import uuid
from statistics import mean

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import TrainingSession
from sobesnik_api.deps import DB, LLM, AppSettings
from sobesnik_api.schemas import (
    DOCS_SHOWN,
    SessionAnswerOut,
    SessionCreate,
    SessionOut,
    SessionQuestionOut,
    SessionSummaryOut,
    SummaryQuestionOut,
    docs_out,
)
from sobesnik_api.services.answers import AnswerState, finish_session, session_answers
from sobesnik_api.services.sessions import create_repeat_session, create_session, get_session

router = APIRouter(tags=["sessions"])


def _answer_out(state: AnswerState | None) -> SessionAnswerOut | None:
    if state is None:
        return None
    evaluation = state.evaluation
    return SessionAnswerOut(
        id=state.answer.id,
        kind=state.answer.kind,
        status=state.answer.status,
        total=evaluation.total if evaluation else None,
        passed=evaluation.passed if evaluation else None,
    )


async def session_out(db: AsyncSession, session: TrainingSession) -> SessionOut:
    answers = await session_answers(db, session.id)
    return SessionOut(
        id=session.id,
        vacancy_id=session.vacancy_id,
        mode=session.mode,
        topics=session.topics,
        status=session.status,
        questions=[
            SessionQuestionOut(
                id=item.question.id,
                position=item.position,
                text=item.question.text,
                requirement_id=item.question.requirement_id,
                topic=item.question.topic_slug,
                key_points=item.question.key_points,
                answer=_answer_out(answers.get(item.question_id)),
                docs=docs_out(item.question.topic_slug, DOCS_SHOWN),
            )
            for item in session.items
        ],
    )


async def summary_out(db: AsyncSession, session: TrainingSession) -> SessionSummaryOut:
    answers = await session_answers(db, session.id)
    questions = []
    for item in session.items:
        state = answers.get(item.question_id)
        evaluation = state.evaluation if state else None
        questions.append(
            SummaryQuestionOut(
                position=item.position,
                question_id=item.question_id,
                answer_status=state.answer.status if state else None,
                total=evaluation.total if evaluation else None,
                passed=evaluation.passed if evaluation else None,
            )
        )
    totals = [q.total for q in questions if q.total is not None]
    return SessionSummaryOut(
        id=session.id,
        status=session.status,
        finished_at=session.finished_at,
        questions=questions,
        answered=sum(q.answer_status is not None for q in questions),
        evaluated=len(totals),
        passed=sum(bool(q.passed) for q in questions),
        avg_total=round(mean(totals), 1) if totals else None,
    )


@router.post("/sessions", status_code=201)
async def post_session(body: SessionCreate, db: DB, llm: LLM, settings: AppSettings) -> SessionOut:
    if body.mode == "repeat":
        assert body.user_id is not None
        session = await create_repeat_session(
            db,
            llm,
            body.user_id,
            body.n,
            settings.progress_thresholds(),
            max_retries=settings.llm_max_retries,
        )
    else:
        assert body.vacancy_id is not None
        session = await create_session(
            db,
            llm,
            body.vacancy_id,
            body.n,
            topics=body.topics,
            max_retries=settings.llm_max_retries,
        )
    return await session_out(db, session)


@router.get("/sessions/{session_id}")
async def read_session(session_id: uuid.UUID, db: DB) -> SessionOut:
    return await session_out(db, await get_session(db, session_id))


@router.post("/sessions/{session_id}/finish")
async def post_finish(session_id: uuid.UUID, db: DB) -> SessionSummaryOut:
    return await summary_out(db, await finish_session(db, session_id))
