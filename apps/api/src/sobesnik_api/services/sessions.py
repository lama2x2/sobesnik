"""Сессия по вакансии или повторение: план → N вопросов от LLM → запись в БД (002 §9, 007 §6)."""

import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import (
    Question,
    Requirement,
    SessionQuestion,
    TrainingSession,
    User,
    Vacancy,
)
from sobesnik_api.errors import ConflictError, NotFoundError, UnprocessableError
from sobesnik_api.services.progress import user_progress
from sobesnik_api.services.vacancies import get_vacancy
from sobesnik_core.checks import question_problems
from sobesnik_core.generation import questions_prompt
from sobesnik_core.limits import QUESTION_MAX_TOKENS, QUESTIONS_BASE_MAX_TOKENS
from sobesnik_core.planning import PlanRequirement, PlanSlot, plan_questions
from sobesnik_core.progress import Thresholds, repeat_topics
from sobesnik_core.prompt import load_prompt
from sobesnik_core.questions import (
    GeneratedQuestion,
    GeneratedQuestions,
    questions_json_schema,
)
from sobesnik_core.topics import topic_title
from sobesnik_core.vacancy import Difficulty, VacancyProfile, shift_level
from sobesnik_llm import LLMProvider, LLMRequest, generate_structured

log = logging.getLogger(__name__)

QUESTIONS_PROMPT = "generate_questions"


def profile_of(vacancy: Vacancy) -> VacancyProfile:
    return VacancyProfile.model_validate(
        {
            "title": vacancy.title,
            "level": vacancy.level,
            "stack": vacancy.stack,
            "requirements": [
                {"text": r.text, "is_required": r.is_required, "topic": r.topic_slug}
                for r in vacancy.requirements
            ],
        }
    )


async def asked_questions(db: AsyncSession, vacancy_id: uuid.UUID) -> list[Question]:
    """Вопросы прошлых сессий вакансии, от старых к новым."""
    result = await db.scalars(
        select(Question)
        .join(SessionQuestion, SessionQuestion.question_id == Question.id)
        .join(TrainingSession, TrainingSession.id == SessionQuestion.session_id)
        .where(TrainingSession.vacancy_id == vacancy_id)
        .order_by(TrainingSession.created_at, SessionQuestion.position)
    )
    return list(result)


@dataclass(frozen=True)
class GeneratedSession:
    slots: list[PlanSlot]
    questions: list[GeneratedQuestion]
    """Вопрос i отвечает слоту плана i."""
    prompt_version: str
    attempts: int


async def generate_questions(
    llm: LLMProvider,
    profile: VacancyProfile,
    n: int,
    *,
    asked: Sequence[str] = (),
    asked_counts: Sequence[int] | None = None,
    max_retries: int = 2,
) -> GeneratedSession:
    """План по требованиям профиля и N вопросов от LLM, без БД.

    `asked` — тексты уже заданных по вакансии вопросов, `asked_counts[i]` — сколько их было
    по требованию i.
    """
    counts = asked_counts or [0] * len(profile.requirements)
    slots = plan_questions(
        [
            PlanRequirement(r.is_required, r.topic, count)
            for r, count in zip(profile.requirements, counts, strict=True)
        ],
        n,
    )
    template = load_prompt(QUESTIONS_PROMPT)
    rendered = questions_prompt(template, profile, slots, asked)

    def check(generated: GeneratedQuestions) -> list[str]:
        if len(generated.questions) < n:
            return [f"questions: нужно ровно {n} вопросов, получено {len(generated.questions)}"]
        return question_problems(generated.questions[:n], asked)

    result = await generate_structured(
        llm,
        LLMRequest(
            system=rendered.system,
            prompt=rendered.user,
            temperature=template.temperature,
            seed=template.seed,
            max_tokens=QUESTIONS_BASE_MAX_TOKENS + QUESTION_MAX_TOKENS * n,
        ),
        GeneratedQuestions,
        json_schema=questions_json_schema(n),
        check=check,
        max_retries=max_retries,
    )
    return GeneratedSession(
        slots=slots,
        questions=result.value.questions[:n],
        prompt_version=template.version,
        attempts=len(result.attempts),
    )


async def create_session(
    db: AsyncSession,
    llm: LLMProvider,
    vacancy_id: uuid.UUID,
    n: int,
    *,
    topics: Sequence[str] | None = None,
    required_only: bool = False,
    difficulty: Difficulty = "same",
    max_retries: int = 2,
) -> TrainingSession:
    """Сессия по вакансии; `topics` — только требования с этими темами (007 §6.1),
    `required_only` — только обязательные, `difficulty` — сдвиг уровня в промпте (010 §5)."""
    vacancy = await get_vacancy(db, vacancy_id)
    requirements = vacancy.requirements
    if topics is not None:
        unknown = set(topics) - {r.topic_slug for r in requirements}
        if unknown:
            raise UnprocessableError(
                "topics_not_in_vacancy",
                f"в вакансии нет тем: {', '.join(sorted(unknown))}",
            )
        requirements = [r for r in requirements if r.topic_slug in set(topics)]
    if required_only:
        requirements = [r for r in requirements if r.is_required]
        if not requirements:
            raise UnprocessableError(
                "no_required_requirements",
                "в вакансии нет обязательных требований"
                + (" по выбранным темам" if topics is not None else ""),
            )
    asked = await asked_questions(db, vacancy.id)
    asked_by_requirement = Counter(q.requirement_id for q in asked)
    profile = profile_of(vacancy)
    profile = profile.model_copy(
        update={
            "requirements": [
                profile.requirements[vacancy.requirements.index(r)] for r in requirements
            ],
            "level": shift_level(profile.level, difficulty),
        }
    )
    generated = await generate_questions(
        llm,
        profile,
        n,
        asked=[q.text for q in asked],
        asked_counts=[asked_by_requirement[r.id] for r in requirements],
        max_retries=max_retries,
    )

    session = TrainingSession(
        user_id=vacancy.user_id,
        vacancy_id=vacancy.id,
        topics=sorted(set(topics)) if topics is not None else None,
        required_only=required_only,
        difficulty=difficulty,
    )
    for position, (slot, item) in enumerate(
        zip(generated.slots, generated.questions, strict=True), start=1
    ):
        requirement = requirements[slot.requirement_index]
        session.items.append(
            SessionQuestion(
                position=position,
                question=_question(llm, generated, item, requirement.id, requirement.topic_slug),
            )
        )
    return await _save(db, session, n, generated.attempts)


def _question(
    llm: LLMProvider,
    generated: GeneratedSession,
    item: GeneratedQuestion,
    requirement_id: uuid.UUID | None,
    topic: str | None,
) -> Question:
    return Question(
        requirement_id=requirement_id,
        topic_slug=topic,
        text=item.text,
        key_points=list(item.key_points),
        gen_model=llm.model,
        prompt_version=generated.prompt_version,
    )


async def _save(
    db: AsyncSession, session: TrainingSession, n: int, attempts: int
) -> TrainingSession:
    db.add(session)
    await db.commit()
    log.info(
        "session created",
        extra={
            "session_id": str(session.id),
            "mode": session.mode,
            "n": n,
            "attempts": attempts,
            "required_only": session.required_only,
            "difficulty": session.difficulty,
        },
    )
    return session


REPEAT_TITLE = "Повторение слабых тем"


@dataclass(frozen=True)
class _RepeatSlot:
    topic: str
    requirement: Requirement | None
    """Требование последнего вопроса пользователя по теме; `None` — его удалили."""
    vacancy: Vacancy | None


async def _last_questions_by_topic(
    db: AsyncSession, user_id: uuid.UUID, topics: Sequence[str]
) -> list[Question]:
    """Все вопросы пользователя по темам, от новых к старым."""
    result = await db.scalars(
        select(Question)
        .join(SessionQuestion, SessionQuestion.question_id == Question.id)
        .join(TrainingSession, TrainingSession.id == SessionQuestion.session_id)
        .where(TrainingSession.user_id == user_id, Question.topic_slug.in_(topics))
        .order_by(TrainingSession.created_at.desc(), SessionQuestion.position.desc())
    )
    return list(result)


async def create_repeat_session(
    db: AsyncSession,
    llm: LLMProvider,
    user_id: uuid.UUID,
    n: int,
    thresholds: Thresholds,
    *,
    difficulty: Difficulty = "same",
    max_retries: int = 2,
) -> TrainingSession:
    """Повторение (PRG-3): по одному новому вопросу на слабую тему, тот же промпт (007 §6.2);
    `difficulty` сдвигает уровень последней вакансии (010 §5.2)."""
    if await db.get(User, user_id) is None:
        raise NotFoundError("user_not_found", "пользователь не найден")
    topics = repeat_topics(await user_progress(db, user_id), thresholds, n)
    if not topics:
        raise ConflictError("nothing_to_repeat", "слабых тем нет — повторять нечего")

    asked = await _last_questions_by_topic(db, user_id, topics)
    slots: list[_RepeatSlot] = []
    for topic in topics:
        last = next((q for q in asked if q.topic_slug == topic), None)
        requirement = (
            await db.get(Requirement, last.requirement_id) if last and last.requirement_id else None
        )
        vacancy = await db.get(Vacancy, requirement.vacancy_id) if requirement else None
        slots.append(_RepeatSlot(topic, requirement, vacancy))

    vacancies = [s.vacancy for s in slots if s.vacancy is not None]
    latest = max(vacancies, key=lambda v: v.created_at) if vacancies else None
    stack = list(dict.fromkeys(item for v in vacancies for item in v.stack))
    profile = VacancyProfile.model_validate(
        {
            "title": REPEAT_TITLE,
            "level": latest.level if latest else "middle",
            "stack": stack,
            "requirements": [
                {
                    "text": s.requirement.text
                    if s.requirement
                    else f"Тема: {topic_title(s.topic) or s.topic}",
                    "is_required": s.requirement.is_required if s.requirement else True,
                    "topic": s.topic,
                }
                for s in slots
            ],
        }
    )
    profile = profile.model_copy(update={"level": shift_level(profile.level, difficulty)})
    generated = await generate_questions(
        llm,
        profile,
        len(slots),
        asked=[q.text for q in reversed(asked)],
        max_retries=max_retries,
    )

    session = TrainingSession(
        user_id=user_id, vacancy_id=None, mode="repeat", difficulty=difficulty
    )
    # план ставит обязательные требования раньше, а повторение идёт от самых слабых тем
    ordered = sorted(
        zip(generated.slots, generated.questions, strict=True),
        key=lambda pair: pair[0].requirement_index,
    )
    for position, (slot, item) in enumerate(ordered, start=1):
        source = slots[slot.requirement_index]
        session.items.append(
            SessionQuestion(
                position=position,
                question=_question(
                    llm,
                    generated,
                    item,
                    source.requirement.id if source.requirement else None,
                    source.topic,
                ),
            )
        )
    return await _save(db, session, len(slots), generated.attempts)


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> TrainingSession:
    session = await db.get(TrainingSession, session_id)
    if session is None:
        raise NotFoundError("session_not_found", "сессия не найдена")
    return session
