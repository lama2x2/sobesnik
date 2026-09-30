"""Сессия по вакансии: план → N вопросов от LLM → запись в БД (002 §9)."""

import logging
import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Question, SessionQuestion, TrainingSession, Vacancy
from sobesnik_api.errors import NotFoundError
from sobesnik_api.services.vacancies import get_vacancy
from sobesnik_core.checks import question_problems
from sobesnik_core.generation import questions_prompt
from sobesnik_core.planning import PlanRequirement, plan_questions
from sobesnik_core.prompt import load_prompt
from sobesnik_core.questions import GeneratedQuestions, questions_json_schema
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
    asked_texts = [q.text for q in asked]
    asked_counts = Counter(q.requirement_id for q in asked)
    slots = plan_questions(
        [PlanRequirement(r.is_required, r.topic_slug, asked_counts[r.id]) for r in requirements],
        n,
    )

    template = load_prompt(QUESTIONS_PROMPT)
    rendered = questions_prompt(template, profile_of(vacancy), slots, asked_texts)

    def check(generated: GeneratedQuestions) -> list[str]:
        if len(generated.questions) < n:
            return [f"questions: нужно ровно {n} вопросов, получено {len(generated.questions)}"]
        return question_problems(generated.questions[:n], asked_texts)

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
    generated = result.value.questions[:n]

    session = TrainingSession(user_id=vacancy.user_id, vacancy_id=vacancy.id)
    for position, (slot, item) in enumerate(zip(slots, generated, strict=True), start=1):
        requirement = requirements[slot.requirement_index]
        question = Question(
            requirement_id=requirement.id,
            topic_slug=requirement.topic_slug,
            text=item.text,
            key_points=list(item.key_points),
            gen_model=llm.model,
            prompt_version=template.version,
        )
        session.items.append(SessionQuestion(position=position, question=question))
    db.add(session)
    await db.commit()
    log.info(
        "session created",
        extra={"session_id": str(session.id), "n": n, "attempts": len(result.attempts)},
    )
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> TrainingSession:
    session = await db.get(TrainingSession, session_id)
    if session is None:
        raise NotFoundError("session_not_found", "сессия не найдена")
    return session
