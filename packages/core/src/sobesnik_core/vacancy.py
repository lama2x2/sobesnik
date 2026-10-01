"""Профиль вакансии: то, что модель извлекает из текста вакансии (002 §8)."""

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sobesnik_core.topics import TopicCatalog

Level = Literal["intern", "junior", "middle", "senior", "lead"]
LEVELS: tuple[Level, ...] = ("intern", "junior", "middle", "senior", "lead")

TextKind = Literal["vacancy", "resume", "other"]


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    is_required: bool
    topic: str | None = None
    """Slug листовой темы. Для модели — обязательная строка; справочник проверяет check_parsed."""


class VacancyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    level: Level
    stack: list[str] = Field(min_length=1, max_length=30)
    requirements: list[Requirement] = Field(min_length=1, max_length=15)


class ParsedVacancy(BaseModel):
    """Схема ответа модели при разборе: сначала вид текста, потом профиль."""

    model_config = ConfigDict(extra="forbid")

    kind: TextKind
    profile: VacancyProfile | None = None


def parse_json_schema() -> dict[str, Any]:
    """JSON Schema разбора, где тема требования — обязательная строка.

    Не enum справочника: грамматика с enum из ~100 слагов вдвое замедляет генерацию в Ollama
    (002 §8.1). Тема вне справочника ловится в check_parsed и уходит на повтор.
    """
    schema = copy.deepcopy(ParsedVacancy.model_json_schema())
    requirement = schema["$defs"]["Requirement"]
    requirement["properties"]["topic"] = {"type": "string"}
    requirement["required"] = ["text", "is_required", "topic"]
    return schema


def repair_parsed(parsed: ParsedVacancy, catalog: TopicCatalog) -> ParsedVacancy:
    """Чинит выдуманные темы известных областей до проверки (002 §8.1)."""
    if parsed.profile is None:
        return parsed
    requirements = [
        r.model_copy(update={"topic": catalog.resolve(r.topic)}) if r.topic else r
        for r in parsed.profile.requirements
    ]
    profile = parsed.profile.model_copy(update={"requirements": requirements})
    return parsed.model_copy(update={"profile": profile})


def repaired_topics(before: ParsedVacancy, after: ParsedVacancy) -> int:
    if before.profile is None or after.profile is None:
        return 0
    pairs = zip(before.profile.requirements, after.profile.requirements, strict=True)
    return sum(a.topic != b.topic for a, b in pairs)


def check_parsed(parsed: ParsedVacancy, catalog: TopicCatalog) -> list[str]:
    """Доменные проверки ответа модели. Пустой список — ответ принят."""
    if parsed.kind != "vacancy":
        return []
    if parsed.profile is None:
        return ["profile: для kind = vacancy профиль обязателен"]
    problems = []
    seen: dict[str, int] = {}
    for i, req in enumerate(parsed.profile.requirements):
        if req.topic is None:
            problems.append(f"profile.requirements.{i}.topic: тема обязательна")
        elif not catalog.is_leaf(req.topic):
            areas = ", ".join(a.slug for a in catalog.areas)
            problems.append(
                f"profile.requirements.{i}.topic: темы {req.topic} нет в справочнике; "
                f"бери slug только из списка тем, области: {areas}"
            )
        key = " ".join(req.text.lower().split())
        if key in seen:
            problems.append(f"profile.requirements.{i}: повторяет требование {seen[key]}")
        seen.setdefault(key, i)
    return problems


def normalize_stack(stack: list[str], aliases: dict[str, str]) -> list[str]:
    """Нижний регистр, синонимы, без дублей; порядок сохраняется."""
    result: list[str] = []
    for item in stack:
        name = " ".join(item.strip().lower().split())
        name = aliases.get(name, name)
        if name and name not in result:
            result.append(name)
    return result


def normalize_requirement_text(text: str) -> str:
    """Заглавная первая буква, если текст начинается со строчной кириллицы.

    Латиницу не трогаем, чтобы не испортить iOS или gRPC.
    """
    text = " ".join(text.split())
    if text[:1] and ("а" <= text[0] <= "я" or text[0] == "ё"):
        return text[0].upper() + text[1:]
    return text


def normalize_profile(profile: VacancyProfile, catalog: TopicCatalog) -> VacancyProfile:
    stack = normalize_stack(profile.stack, catalog.stack_aliases) or profile.stack
    requirements = [
        r.model_copy(update={"text": normalize_requirement_text(r.text)})
        for r in profile.requirements
    ]
    return profile.model_copy(update={"stack": stack, "requirements": requirements})
