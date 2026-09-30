import uuid

from fastapi import APIRouter

from sobesnik_api.db.models import TrainingSession
from sobesnik_api.deps import DB, LLM, AppSettings
from sobesnik_api.schemas import SessionCreate, SessionOut, SessionQuestionOut
from sobesnik_api.services.sessions import create_session, get_session

router = APIRouter(tags=["sessions"])


def session_out(session: TrainingSession) -> SessionOut:
    return SessionOut(
        id=session.id,
        vacancy_id=session.vacancy_id,
        mode=session.mode,
        status=session.status,
        questions=[
            SessionQuestionOut(
                id=item.question.id,
                position=item.position,
                text=item.question.text,
                requirement_id=item.question.requirement_id,
                topic=item.question.topic_slug,
                key_points=item.question.key_points,
            )
            for item in session.items
        ],
    )


@router.post("/sessions", status_code=201)
async def post_session(body: SessionCreate, db: DB, llm: LLM, settings: AppSettings) -> SessionOut:
    session = await create_session(
        db, llm, body.vacancy_id, body.n, max_retries=settings.llm_max_retries
    )
    return session_out(session)


@router.get("/sessions/{session_id}")
async def read_session(session_id: uuid.UUID, db: DB) -> SessionOut:
    return session_out(await get_session(db, session_id))
