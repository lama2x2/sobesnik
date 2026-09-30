"""Профиль вакансии: то, что модель извлекает из текста вакансии."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["intern", "junior", "middle", "senior", "lead"]
LEVELS: tuple[Level, ...] = ("intern", "junior", "middle", "senior", "lead")


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    is_required: bool


class VacancyProfile(BaseModel):
    """Схема ответа модели при разборе вакансии и одновременно её валидатор."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    level: Level
    stack: list[str] = Field(min_length=1, max_length=30)
    requirements: list[Requirement] = Field(min_length=1, max_length=15)
