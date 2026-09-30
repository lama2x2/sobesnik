"""Сессия по вакансии: план → N вопросов от LLM → запись в БД (002 §9)."""

import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Question, SessionQuestion, TrainingSession, Vacancy
from sobesnik_api.errors import NotFoundError
from sobesnik_api.services.vacancies import get_vacancy
from sobesnik_core.checks import question_problems
from sobesnik_core.generation import questions_prompt
from sobesnik_core.planning import PlanRequirement, PlanSlot, plan_questions
from sobesnik_core.prompt import load_prompt
from sobesnik_core.questions import (
    GeneratedQuestion,
    GeneratedQuestions,
    questions_json_schema,
)
from sobesnik_core.vacancy import VacancyProfile
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
    max_retries: int = 2,
) -> TrainingSession:
    vacancy = await get_vacancy(db, vacancy_id)
    requirements = vacancy.requirements
    asked = await asked_questions(db, vacancy.id)
    asked_by_requirement = Counter(q.requirement_id for q in asked)
    generated = await generate_questions(
        llm,
        profile_of(vacancy),
        n,
        asked=[q.text for q in asked],
        asked_counts=[asked_by_requirement[r.id] for r in requirements],
        max_retries=max_retries,
    )

    session = TrainingSession(user_id=vacancy.user_id, vacancy_id=vacancy.id)
    for position, (slot, item) in enumerate(
        zip(generated.slots, generated.questions, strict=True), start=1
    ):
        requirement = requirements[slot.requirement_index]
        question = Question(
            requirement_id=requirement.id,
            topic_slug=requirement.topic_slug,
            text=item.text,
            key_points=list(item.key_points),
            gen_model=llm.model,
            prompt_version=generated.prompt_version,
        )
        session.items.append(SessionQuestion(position=position, question=question))
    db.add(session)
    await db.commit()
    log.info(
        "session created",
        extra={"session_id": str(session.id), "n": n, "attempts": generated.attempts},
    )
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> TrainingSession:
    session = await db.get(TrainingSession, session_id)
    if session is None:
        raise NotFoundError("session_not_found", "сессия не найдена")
    return session
