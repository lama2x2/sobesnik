from fastapi import APIRouter

from sobesnik_api.deps import DB, LLM
from sobesnik_api.schemas import SessionCreate, SessionOut, SessionQuestionOut
from sobesnik_api.services.sessions import create_session

router = APIRouter(tags=["sessions"])


@router.post("/sessions", status_code=201)
async def post_session(body: SessionCreate, db: DB, llm: LLM) -> SessionOut:
    session = await create_session(db, llm, body.vacancy_id, body.n)
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
            )
            for item in session.items
        ],
    )
