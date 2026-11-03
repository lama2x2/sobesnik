import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from sobesnik_api.auth import CallerDep
from sobesnik_api.db.models import TrainingSession, User, Vacancy
from sobesnik_api.deps import DB, AppSettings, Storage
from sobesnik_api.errors import NotFoundError
from sobesnik_api.routes.sessions import summary_out
from sobesnik_api.schemas import (
    ForgetOut,
    NextDueOut,
    ProgressOut,
    SessionHistoryOut,
    ThresholdsOut,
    TopicProgressOut,
    UserOut,
    VacancyBriefOut,
    docs_out,
)
from sobesnik_api.services.progress import forget_rest, user_progress
from sobesnik_api.services.voice import forget_audio
from sobesnik_core.progress import TopicStats, is_low, next_due, sort_key, topic_status
from sobesnik_core.repetition import due_in
from sobesnik_core.topics import topic_title

router = APIRouter(tags=["users"])


@router.put("/users/telegram/{telegram_id}", response_model=UserOut)
async def upsert_telegram_user(
    telegram_id: int, db: DB, response: Response, caller: CallerDep
) -> User:
    caller.require_service()
    created = await db.scalar(
        insert(User)
        .values(telegram_id=telegram_id)
        .on_conflict_do_nothing(index_elements=[User.telegram_id])
        .returning(User.id)
    )
    await db.commit()
    user = await db.scalar(select(User).where(User.telegram_id == telegram_id))
    assert user is not None
    response.status_code = 201 if created is not None else 200
    return user


@router.delete("/users/{user_id}/data", response_model_exclude_none=True)
async def forget_user_data(
    user_id: uuid.UUID,
    scope: Literal["audio", "all"],
    db: DB,
    storage: Storage,
    caller: CallerDep,
) -> ForgetOut:
    """`audio` — голосовые и расшифровки (ASR-5, 004 §8.5); `all` — всё о пользователе (007 §7.3).

    Сначала удаляется аудио из хранилища: если оно недоступно, в БД ничего не трогается.
    """
    caller.check_user(user_id)
    audio = await forget_audio(db, storage, user_id)
    out = ForgetOut(
        audio_deleted=audio.audio_deleted,
        transcripts_deleted=audio.transcripts_deleted,
        reviews_deleted=audio.reviews_deleted,
    )
    if scope == "all":
        rest = await forget_rest(db, user_id)
        out.answers_deleted = rest.answers
        out.sessions_deleted = rest.sessions
        out.vacancies_deleted = rest.vacancies
    return out


@router.get("/users/{user_id}/progress")
async def read_progress(
    user_id: uuid.UUID,
    db: DB,
    settings: AppSettings,
    caller: CallerDep,
    sessions_limit: Annotated[int, Query(ge=0, le=20)] = 5,
) -> ProgressOut:
    """Прогресс по темам, слабые темы и последние тренировки (С4, PRG-4, 007 §7.1)."""
    caller.check_user(user_id)
    if await db.get(User, user_id) is None:
        raise NotFoundError("user_not_found", "пользователь не найден")
    thresholds = settings.progress_thresholds()
    progress = await user_progress(db, user_id)
    now = datetime.now(UTC)
    topics = [
        TopicProgressOut(
            slug=slug,
            title=topic_title(slug) or slug,
            status=topic_status(stats, thresholds),
            low=is_low(stats, thresholds),
            attempts=stats.attempts,
            last_score=stats.last_score,
            avg_last3=round(stats.avg_last3, 1) if stats.avg_last3 is not None else None,
            docs=docs_out(slug),
            due=stats.due(now),
            due_at=stats.schedule.due_at if stats.schedule else None,
            due_in=due_in(stats.schedule.due_at, now) if stats.schedule else "сейчас",
            interval_days=stats.schedule.interval_days if stats.schedule else None,
        )
        for slug, stats in sorted(
            progress.items(), key=lambda item: (*sort_key(item[1], thresholds), item[0])
        )
    ]
    sessions = await db.scalars(
        select(TrainingSession)
        .where(TrainingSession.user_id == user_id)
        .order_by(TrainingSession.created_at.desc(), TrainingSession.id)
        .limit(sessions_limit)
    )
    history = []
    for session in sessions:
        summary = await summary_out(db, session)
        vacancy = await db.get(Vacancy, session.vacancy_id) if session.vacancy_id else None
        history.append(
            SessionHistoryOut(
                id=session.id,
                created_at=session.created_at,
                mode=session.mode,
                status=session.status,
                vacancy_title=vacancy.title if vacancy else None,
                answered=summary.answered,
                passed=summary.passed,
                avg_total=summary.avg_total,
                scores=[q.total for q in summary.questions],
            )
        )
    return ProgressOut(
        thresholds=ThresholdsOut(
            min_attempts=thresholds.min_attempts,
            weak_avg=thresholds.weak_avg,
            low_score=thresholds.low_score,
        ),
        topics=topics,
        sessions=history,
        due_count=sum(t.due for t in topics),
        next_due=_next_due(progress, now) if not any(t.due for t in topics) else None,
    )


def _next_due(progress: dict[str, TopicStats], now: datetime) -> NextDueOut | None:
    nearest = next_due(progress)
    if nearest is None:
        return None
    slug, due_at = nearest
    return NextDueOut(
        slug=slug, title=topic_title(slug) or slug, due_at=due_at, due_in=due_in(due_at, now)
    )


@router.get("/users/{user_id}/vacancies")
async def read_vacancies(
    user_id: uuid.UUID,
    db: DB,
    caller: CallerDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[VacancyBriefOut]:
    """Вакансии пользователя, новые сверху (009 §9): веб показывает их на главной."""
    caller.check_user(user_id)
    if await db.get(User, user_id) is None:
        raise NotFoundError("user_not_found", "пользователь не найден")
    vacancies = await db.scalars(
        select(Vacancy)
        .where(Vacancy.user_id == user_id)
        .order_by(Vacancy.created_at.desc(), Vacancy.id)
        .limit(limit)
    )
    return [VacancyBriefOut.model_validate(v) for v in vacancies]
