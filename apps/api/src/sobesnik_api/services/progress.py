"""Прогресс по темам: пересчёт после оценки и чтение (PRG-1, PRG-4, 007 §4, §7.1)."""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import (
    Answer,
    Evaluation,
    Question,
    SessionQuestion,
    TopicProgress,
    TrainingSession,
    User,
    Vacancy,
)
from sobesnik_core.progress import LAST_N, TopicStats, stats_from_history
from sobesnik_core.repetition import Schedule

log = logging.getLogger(__name__)


def _attempts_query(user_id: uuid.UUID) -> Select[Any]:
    """Попытки пользователя: его ответы с боевой оценкой `done` на вопросы с темой (007 §4).

    У ответа одна боевая оценка (003 §7); если их почему-то несколько — берётся последняя.
    """
    live = (
        select(
            Evaluation.answer_id,
            Evaluation.total,
            func.row_number()
            .over(
                partition_by=Evaluation.answer_id,
                order_by=(Evaluation.created_at.desc(), Evaluation.id),
            )
            .label("rn"),
        )
        .where(Evaluation.status == "done", Evaluation.eval_run_id.is_(None))
        .subquery()
    )
    return (
        select(Question.topic_slug, live.c.total, Answer.created_at)
        .join(live, live.c.answer_id == Answer.id)
        .join(Question, Question.id == Answer.question_id)
        .where(
            Answer.user_id == user_id,
            Answer.source == "user",
            live.c.rn == 1,
            live.c.total.is_not(None),
            Question.topic_slug.is_not(None),
        )
    )


async def recompute_topic(db: AsyncSession, user_id: uuid.UUID, topic: str) -> TopicStats | None:
    """Пересчитывает строку темы из БД заново: повтор задачи ничего не задваивает. Без коммита."""
    rows = (
        await db.execute(
            _attempts_query(user_id)
            .where(Question.topic_slug == topic)
            .order_by(Answer.created_at.desc(), Answer.id)
        )
    ).all()
    history = [(int(r.total), r.created_at) for r in rows]
    if not history:
        await db.execute(
            delete(TopicProgress).where(
                TopicProgress.user_id == user_id, TopicProgress.topic_slug == topic
            )
        )
        return None
    stats = stats_from_history(history)
    await _upsert(db, user_id, topic, stats)
    return stats


async def _upsert(db: AsyncSession, user_id: uuid.UUID, topic: str, stats: TopicStats) -> None:
    sched = stats.schedule
    values = {
        "attempts": stats.attempts,
        "last_scores": list(stats.last_scores),
        "last_score": stats.last_score,
        "avg_last3": stats.avg_last3,
        "reps": sched.reps if sched else None,
        "ease": sched.ease if sched else None,
        "interval_days": sched.interval_days if sched else None,
        "due_at": sched.due_at if sched else None,
        "updated_at": func.now(),
    }
    await db.execute(
        insert(TopicProgress)
        .values(user_id=user_id, topic_slug=topic, **values)
        .on_conflict_do_update(index_elements=["user_id", "topic_slug"], set_=values)
    )


async def update_after_evaluation(db: AsyncSession, answer: Answer) -> None:
    """Вызывается воркером перед коммитом статуса `done` (007 §4)."""
    if answer.source != "user" or answer.user_id is None or answer.question.topic_slug is None:
        return
    await db.flush()
    await recompute_topic(db, answer.user_id, answer.question.topic_slug)


async def rebuild(db: AsyncSession, user_id: uuid.UUID | None = None) -> int:
    """Пересчитывает прогресс всех (или одного) пользователей заново. Возвращает число строк."""
    users = (
        [user_id]
        if user_id
        else list(
            await db.scalars(select(Answer.user_id).where(Answer.user_id.is_not(None)).distinct())
        )
    )
    count = 0
    for uid in users:
        assert uid is not None
        rows = (
            await db.execute(_attempts_query(uid).order_by(Answer.created_at.desc(), Answer.id))
        ).all()
        by_topic: dict[str, list[tuple[int, datetime]]] = defaultdict(list)
        for r in rows:
            by_topic[r.topic_slug].append((int(r.total), r.created_at))
        await db.execute(delete(TopicProgress).where(TopicProgress.user_id == uid))
        for topic, history in by_topic.items():
            await _upsert(db, uid, topic, stats_from_history(history))
            count += 1
    await db.commit()
    log.info("progress rebuilt", extra={"rows": count})
    return count


async def user_progress(db: AsyncSession, user_id: uuid.UUID) -> dict[str, TopicStats]:
    rows = await db.scalars(select(TopicProgress).where(TopicProgress.user_id == user_id))
    return {r.topic_slug: _stats(r) for r in rows}


def _stats(row: TopicProgress) -> TopicStats:
    sched = None
    if row.due_at is not None:
        assert row.reps is not None and row.ease is not None and row.interval_days is not None
        sched = Schedule(row.reps, row.ease, row.interval_days, row.due_at)
    return TopicStats(
        attempts=row.attempts, last_scores=tuple(row.last_scores[:LAST_N]), schedule=sched
    )


@dataclass(frozen=True)
class ForgetAllResult:
    answers: int
    sessions: int
    vacancies: int


async def forget_rest(db: AsyncSession, user_id: uuid.UUID) -> ForgetAllResult:
    """Удаляет всё, кроме аудио (его удаляет `forget_audio` раньше): ответы с оценками, сессии
    с вопросами, вакансии с требованиями, прогресс (007 §7.3). Запись `users` остаётся."""
    session_ids = list(
        await db.scalars(select(TrainingSession.id).where(TrainingSession.user_id == user_id))
    )
    question_ids = list(
        await db.scalars(
            select(SessionQuestion.question_id).where(SessionQuestion.session_id.in_(session_ids))
        )
    )
    answers = await db.execute(delete(Answer).where(Answer.user_id == user_id).returning(Answer.id))
    answers_deleted = len(answers.all())
    await db.execute(delete(TrainingSession).where(TrainingSession.id.in_(session_ids)))
    await db.execute(delete(Question).where(Question.id.in_(question_ids)))
    vacancies = await db.execute(
        delete(Vacancy).where(Vacancy.user_id == user_id).returning(Vacancy.id)
    )
    vacancies_deleted = len(vacancies.all())
    await db.execute(delete(TopicProgress).where(TopicProgress.user_id == user_id))
    user = await db.get(User, user_id)
    if user is not None:
        user.settings = {}
    await db.commit()
    log.info(
        "user data forgotten",
        extra={
            "user_id": str(user_id),
            "answers": answers_deleted,
            "sessions": len(session_ids),
            "vacancies": vacancies_deleted,
        },
    )
    return ForgetAllResult(answers_deleted, len(session_ids), vacancies_deleted)
