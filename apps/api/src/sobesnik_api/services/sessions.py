"""Сессия по вакансии: профиль → N вопросов от LLM → запись в БД."""

import logging
import uuid

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api import prompts
from sobesnik_api.db.models import Question, SessionQuestion, TrainingSession, Vacancy
from sobesnik_api.errors import LLMBadOutputError, NotFoundError
from sobesnik_core.questions import GeneratedQuestion, GeneratedQuestions, questions_json_schema
from sobesnik_core.vacancy import VacancyProfile
from sobesnik_llm import LLMProvider, LLMRequest

log = logging.getLogger(__name__)


def profile_of(vacancy: Vacancy) -> VacancyProfile:
    return VacancyProfile.model_validate(
        {
            "title": vacancy.title,
            "level": vacancy.level,
            "stack": vacancy.stack,
            "requirements": [
                {"text": r.text, "is_required": r.is_required} for r in vacancy.requirements
            ],
        }
    )


async def generate_questions(
    llm: LLMProvider, profile: VacancyProfile, n: int
) -> list[GeneratedQuestion]:
    response = await llm.generate(
        LLMRequest(
            system=prompts.GENERATE_QUESTIONS_SYSTEM,
            prompt=prompts.generate_questions_prompt(profile, n),
            json_schema=questions_json_schema(n),
            temperature=0.7,
        )
    )
    log.info(
        "questions generated",
        extra={
            "llm_model": response.model,
            "latency_ms": response.latency_ms,
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
        },
    )
    try:
        questions = GeneratedQuestions.model_validate_json(response.text).questions
    except ValidationError as exc:
        raise LLMBadOutputError(
            f"модель вернула вопросы не по схеме: {exc.error_count()} ошибок"
        ) from exc
    if len(questions) < n:
        raise LLMBadOutputError(f"модель вернула {len(questions)} вопросов вместо {n}")
    return questions[:n]


async def create_session(
    db: AsyncSession, llm: LLMProvider, vacancy_id: uuid.UUID, n: int
) -> TrainingSession:
    vacancy = await db.get(Vacancy, vacancy_id)
    if vacancy is None:
        raise NotFoundError("vacancy_not_found", "вакансия не найдена")
    generated = await generate_questions(llm, profile_of(vacancy), n)

    requirements = vacancy.requirements
    session = TrainingSession(user_id=vacancy.user_id, vacancy_id=vacancy.id)
    for position, item in enumerate(generated, start=1):
        index = item.requirement_index
        requirement = (
            requirements[index] if index is not None and 0 <= index < len(requirements) else None
        )
        question = Question(
            requirement_id=requirement.id if requirement else None,
            text=item.text,
            gen_model=llm.model,
            prompt_version=prompts.GENERATE_QUESTIONS_VERSION,
        )
        session.items.append(SessionQuestion(position=position, question=question))
    db.add(session)
    await db.commit()
    log.info("session created", extra={"session_id": str(session.id), "n": n})
    return session
