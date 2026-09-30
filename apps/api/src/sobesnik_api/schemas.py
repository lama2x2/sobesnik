"""Схемы запросов и ответов API (001-skeleton §8)."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sobesnik_core.limits import MAX_VACANCY_CHARS, QUESTIONS_DEFAULT, QUESTIONS_MAX, QUESTIONS_MIN
from sobesnik_core.vacancy import Level


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    telegram_id: int | None
    created_at: datetime


class VacancyCreate(BaseModel):
    user_id: uuid.UUID
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_VACANCY_CHARS)
    ]


class RequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    text: str
    is_required: bool


class VacancyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    level: Level
    stack: list[str]
    requirements: list[RequirementOut]
    parser_model: str
    prompt_version: str
    created_at: datetime


class SessionCreate(BaseModel):
    vacancy_id: uuid.UUID
    n: int = Field(default=QUESTIONS_DEFAULT, ge=QUESTIONS_MIN, le=QUESTIONS_MAX)


class SessionQuestionOut(BaseModel):
    id: uuid.UUID
    position: int
    text: str
    requirement_id: uuid.UUID | None


class SessionOut(BaseModel):
    id: uuid.UUID
    vacancy_id: uuid.UUID | None
    mode: str
    status: str
    questions: list[SessionQuestionOut]
