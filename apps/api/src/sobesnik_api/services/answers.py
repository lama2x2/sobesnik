"""Ответ на вопрос сессии и его оценка воркером (003 §8). Голосовой ответ — в `voice.py`."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Answer, Evaluation, TrainingSession
from sobesnik_api.errors import ConflictError, NotFoundError
from sobesnik_api.queue import TaskQueue
from sobesnik_api.services.evaluation import live_evaluation
from sobesnik_api.services.sessions import get_session

log = logging.getLogger(__name__)

IN_PROGRESS = ("received", "transcribing", "evaluating")


async def check_can_answer(
    db: AsyncSession, session_id: uuid.UUID, question_id: uuid.UUID
) -> TrainingSession:
    """Вопрос входит в незавершённую сессию, и на него ещё нет ответа (кроме упавшего)."""
    session = await get_session(db, session_id)
    if all(item.question_id != question_id for item in session.items):
        raise NotFoundError("question_not_in_session", "в сессии нет такого вопроса")
    if session.status == "finished":
        raise ConflictError("session_finished", "сессия уже завершена")
    previous = await db.scalars(
        select(Answer.id).where(
            Answer.session_id == session_id,
            Answer.question_id == question_id,
            Answer.status != "failed",
        )
    )
    if previous.first() is not None:
        raise ConflictError("already_answered", "на этот вопрос уже есть ответ")
    return session


async def submit_answer(
    db: AsyncSession,
    queue: TaskQueue,
    session_id: uuid.UUID,
    question_id: uuid.UUID,
    text: str,
) -> Answer:
    """Сохраняет ответ и ставит оценку в очередь. Недоступная очередь не мешает приёму:
    ответ подхватит воркер при старте."""
    session = await check_can_answer(db, session_id, question_id)
    answer = Answer(
        question_id=question_id,
        session_id=session_id,
        user_id=session.user_id,
        source="user",
        kind="text",
        text=text,
        status="received",
    )
    db.add(answer)
    await db.commit()
    try:
        await queue.enqueue_evaluation(answer.id)
    except Exception as exc:  # Redis недоступен — ответ уже сохранён
        log.warning(
            "enqueue failed", extra={"answer_id": str(answer.id), "error": type(exc).__name__}
        )
    log.info("answer received", extra={"answer_id": str(answer.id), "chars": len(text)})
    return answer


async def get_answer(db: AsyncSession, answer_id: uuid.UUID) -> Answer:
    answer = await db.get(Answer, answer_id)
    if answer is None:
        raise NotFoundError("answer_not_found", "ответ не найден")
    return answer


@dataclass(frozen=True)
class AnswerState:
    answer: Answer
    evaluation: Evaluation | None


async def session_answers(db: AsyncSession, session_id: uuid.UUID) -> dict[uuid.UUID, AnswerState]:
    """Последний ответ на каждый вопрос сессии и его «боевая» оценка."""
    answers = await db.scalars(
        select(Answer).where(Answer.session_id == session_id).order_by(Answer.created_at, Answer.id)
    )
    latest: dict[uuid.UUID, Answer] = {}
    for answer in answers:
        latest[answer.question_id] = answer
    return {
        qid: AnswerState(answer, await live_evaluation(db, answer.id))
        for qid, answer in latest.items()
    }


async def finish_session(db: AsyncSession, session_id: uuid.UUID) -> TrainingSession:
    """Завершает сессию. Повторный вызов ничего не меняет."""
    session = await get_session(db, session_id)
    if session.status != "finished":
        session.status = "finished"
        session.finished_at = datetime.now(UTC)
        await db.commit()
        log.info("session finished", extra={"session_id": str(session.id)})
    return session
