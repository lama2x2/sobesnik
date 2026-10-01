"""Вакансия: текст, ссылка или HTML → текст → профиль от LLM → запись в БД (002 §8)."""

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api import errors
from sobesnik_api.db.models import Requirement, User, Vacancy
from sobesnik_api.errors import NotFoundError, UnprocessableError
from sobesnik_api.schemas import VacancyCreate, VacancyPatch
from sobesnik_api.sources.extract import extract_vacancy_text
from sobesnik_api.sources.fetch import PageFetcher, UrlNotAllowedError, UrlUnreadableError
from sobesnik_core.generation import parse_prompt
from sobesnik_core.limits import MIN_VACANCY_CHARS, PARSE_MAX_TOKENS
from sobesnik_core.prompt import load_prompt
from sobesnik_core.topics import load_topics
from sobesnik_core.vacancy import (
    ParsedVacancy,
    VacancyProfile,
    check_parsed,
    normalize_profile,
    parse_json_schema,
    repair_parsed,
    repaired_topics,
)
from sobesnik_llm import LLMProvider, LLMRequest, generate_structured

log = logging.getLogger(__name__)

PARSE_PROMPT = "parse_vacancy"


@dataclass(frozen=True)
class VacancyText:
    text: str
    source: str
    source_url: str | None = None


@dataclass(frozen=True)
class ParseResult:
    profile: VacancyProfile
    prompt_version: str
    repaired_topics: int = 0
    """Сколько выдуманных тем заменено на `<область>.general`."""


async def resolve_text(body: VacancyCreate, fetcher: PageFetcher) -> VacancyText:
    """Текст вакансии из запроса. Ошибки ссылки и HTML — 422 с просьбой прислать текст."""
    if body.text is not None:
        return VacancyText(text=body.text, source="text")
    if body.url is not None:
        try:
            page = await fetcher.fetch(body.url)
        except UrlNotAllowedError as exc:
            log.info("vacancy url rejected", extra={"reason": str(exc)})
            raise UnprocessableError("url_not_allowed", errors.URL_NOT_ALLOWED) from exc
        except UrlUnreadableError as exc:
            log.info("vacancy url unreadable", extra={"reason": str(exc)})
            raise UnprocessableError("url_unreadable", errors.URL_UNREADABLE) from exc
        text = await asyncio.to_thread(extract_vacancy_text, page.content, page.url, page.charset)
        if not text or len(text) < MIN_VACANCY_CHARS:
            log.info("vacancy url unreadable", extra={"reason": "на странице нет текста вакансии"})
            raise UnprocessableError("url_unreadable", errors.URL_UNREADABLE)
        return VacancyText(text=text, source="url", source_url=body.url)
    assert body.html is not None
    text = await asyncio.to_thread(extract_vacancy_text, body.html)
    if not text or len(text) < MIN_VACANCY_CHARS:
        raise UnprocessableError("html_unreadable", errors.HTML_UNREADABLE)
    return VacancyText(text=text, source="html")


async def parse_vacancy(llm: LLMProvider, text: str, *, max_retries: int = 2) -> ParseResult:
    if len(text) < MIN_VACANCY_CHARS:
        raise UnprocessableError("not_a_vacancy", errors.NOT_A_VACANCY_OTHER)
    catalog = load_topics()
    template = load_prompt(PARSE_PROMPT)
    rendered = parse_prompt(template, catalog, text)
    repaired = 0

    def repair(parsed: ParsedVacancy) -> ParsedVacancy:
        nonlocal repaired
        fixed = repair_parsed(parsed, catalog)
        repaired = repaired_topics(parsed, fixed)
        return fixed

    result = await generate_structured(
        llm,
        LLMRequest(
            system=rendered.system,
            prompt=rendered.user,
            temperature=template.temperature,
            seed=template.seed,
            max_tokens=PARSE_MAX_TOKENS,
        ),
        ParsedVacancy,
        json_schema=parse_json_schema(),
        repair=repair,
        check=lambda parsed: check_parsed(parsed, catalog),
        max_retries=max_retries,
    )
    parsed = result.value
    log.info(
        "vacancy parsed",
        extra={
            "kind": parsed.kind,
            "attempts": len(result.attempts),
            "repaired_topics": repaired,
        },
    )
    if parsed.kind == "resume":
        raise UnprocessableError("not_a_vacancy", errors.NOT_A_VACANCY_RESUME)
    if parsed.kind != "vacancy" or parsed.profile is None:
        raise UnprocessableError("not_a_vacancy", errors.NOT_A_VACANCY_OTHER)
    return ParseResult(
        profile=normalize_profile(parsed.profile, catalog),
        prompt_version=template.version,
        repaired_topics=repaired,
    )


async def create_vacancy(
    db: AsyncSession,
    llm: LLMProvider,
    fetcher: PageFetcher,
    body: VacancyCreate,
    *,
    max_retries: int = 2,
) -> Vacancy:
    if await db.get(User, body.user_id) is None:
        raise NotFoundError("user_not_found", "пользователь не найден")
    source = await resolve_text(body, fetcher)
    parsed = await parse_vacancy(llm, source.text, max_retries=max_retries)
    profile = parsed.profile
    vacancy = Vacancy(
        user_id=body.user_id,
        raw_text=source.text,
        source=source.source,
        source_url=source.source_url,
        title=profile.title,
        level=profile.level,
        stack=profile.stack,
        parser_model=llm.model,
        prompt_version=parsed.prompt_version,
        requirements=[
            Requirement(position=i, text=r.text, is_required=r.is_required, topic_slug=r.topic)
            for i, r in enumerate(profile.requirements)
        ],
    )
    db.add(vacancy)
    await db.commit()
    await db.refresh(vacancy)
    log.info("vacancy created", extra={"vacancy_id": str(vacancy.id), "source": source.source})
    return vacancy


async def get_vacancy(db: AsyncSession, vacancy_id: uuid.UUID) -> Vacancy:
    vacancy = await db.get(Vacancy, vacancy_id)
    if vacancy is None:
        raise NotFoundError("vacancy_not_found", "вакансия не найдена")
    return vacancy


async def patch_vacancy(db: AsyncSession, vacancy_id: uuid.UUID, patch: VacancyPatch) -> Vacancy:
    """VAC-4: правка уровня и удаление требований. Вопросы по удалённым требованиям остаются."""
    vacancy = await get_vacancy(db, vacancy_id)
    if patch.level is not None:
        vacancy.level = patch.level
    if patch.remove_requirements:
        remove = set(patch.remove_requirements)
        existing = {r.id for r in vacancy.requirements}
        if not remove <= existing:
            raise UnprocessableError(
                "requirement_not_found", "среди требований вакансии нет таких id"
            )
        if remove == existing:
            raise UnprocessableError(
                "no_requirements_left", "нельзя удалить все требования вакансии"
            )
        vacancy.requirements = [r for r in vacancy.requirements if r.id not in remove]
    await db.commit()
    await db.refresh(vacancy)
    return vacancy
