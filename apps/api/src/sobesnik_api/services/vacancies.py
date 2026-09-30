"""Разбор вакансии: текст → профиль от LLM → запись в БД."""

import logging
import uuid

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api import prompts
from sobesnik_api.db.models import Requirement, User, Vacancy
from sobesnik_api.errors import LLMBadOutputError, NotFoundError
from sobesnik_core.vacancy import VacancyProfile
from sobesnik_llm import LLMProvider, LLMRequest

log = logging.getLogger(__name__)


async def parse_vacancy(llm: LLMProvider, text: str) -> VacancyProfile:
    response = await llm.generate(
        LLMRequest(
            system=prompts.PARSE_VACANCY_SYSTEM,
            prompt=prompts.parse_vacancy_prompt(text),
            json_schema=VacancyProfile.model_json_schema(),
            temperature=0.1,
        )
    )
    log.info(
        "vacancy parsed",
        extra={
            "llm_model": response.model,
            "latency_ms": response.latency_ms,
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
        },
    )
    try:
        return VacancyProfile.model_validate_json(response.text)
    except ValidationError as exc:
        raise LLMBadOutputError(
            f"модель вернула профиль не по схеме: {exc.error_count()} ошибок"
        ) from exc


async def create_vacancy(
    db: AsyncSession, llm: LLMProvider, user_id: uuid.UUID, text: str
) -> Vacancy:
    if await db.get(User, user_id) is None:
        raise NotFoundError("user_not_found", "пользователь не найден")
    profile = await parse_vacancy(llm, text)
    vacancy = Vacancy(
        user_id=user_id,
        raw_text=text,
        title=profile.title,
        level=profile.level,
        stack=profile.stack,
        parser_model=llm.model,
        prompt_version=prompts.PARSE_VACANCY_VERSION,
        requirements=[
            Requirement(position=i, text=r.text, is_required=r.is_required)
            for i, r in enumerate(profile.requirements)
        ],
    )
    db.add(vacancy)
    await db.commit()
    await db.refresh(vacancy)
    log.info("vacancy created", extra={"vacancy_id": str(vacancy.id)})
    return vacancy
