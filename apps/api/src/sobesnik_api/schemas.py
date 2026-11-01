"""Схемы запросов и ответов API (001 §8, 002 §11, 003 §10)."""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sobesnik_core.limits import (
    ANSWER_MAX_CHARS,
    MAX_VACANCY_CHARS,
    QUESTIONS_DEFAULT,
    QUESTIONS_MAX,
    QUESTIONS_MIN,
)
from sobesnik_core.vacancy import Level

MAX_HTML_CHARS = 3 * 1024 * 1024
MAX_URL_CHARS = 2_000


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    telegram_id: int | None
    created_at: datetime


class VacancyCreate(BaseModel):
    """Ровно одно из полей: text, url, html."""

    user_id: uuid.UUID
    text: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_VACANCY_CHARS),
        ]
        | None
    ) = None
    url: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_URL_CHARS)
        ]
        | None
    ) = None
    html: Annotated[str, StringConstraints(min_length=1, max_length=MAX_HTML_CHARS)] | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> Self:
        given = [f for f in ("text", "url", "html") if getattr(self, f) is not None]
        if len(given) != 1:
            raise ValueError("нужно ровно одно из полей: text, url, html")
        return self


class VacancyPatch(BaseModel):
    level: Level | None = None
    remove_requirements: list[uuid.UUID] | None = None

    @model_validator(mode="after")
    def not_empty(self) -> Self:
        if self.level is None and not self.remove_requirements:
            raise ValueError("нечего менять: укажите level или remove_requirements")
        return self


class RequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    text: str
    is_required: bool
    topic: str | None = Field(validation_alias="topic_slug")


class VacancyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    level: Level
    stack: list[str]
    requirements: list[RequirementOut]
    source: str
    source_url: str | None
    parser_model: str
    prompt_version: str
    created_at: datetime


class SessionCreate(BaseModel):
    vacancy_id: uuid.UUID
    n: int = Field(default=QUESTIONS_DEFAULT, ge=QUESTIONS_MIN, le=QUESTIONS_MAX)


class SessionAnswerOut(BaseModel):
    """Последний ответ на вопрос сессии."""

    id: uuid.UUID
    status: str
    total: int | None
    passed: bool | None


class SessionQuestionOut(BaseModel):
    id: uuid.UUID
    position: int
    text: str
    requirement_id: uuid.UUID | None
    topic: str | None
    key_points: list[str]
    answer: SessionAnswerOut | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    vacancy_id: uuid.UUID | None
    mode: str
    status: str
    questions: list[SessionQuestionOut]


class AnswerCreate(BaseModel):
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=ANSWER_MAX_CHARS)
    ]


class AnswerAccepted(BaseModel):
    answer_id: uuid.UUID
    status: str


class CriterionScoreOut(BaseModel):
    criterion: str
    title: str
    weight: float
    score: int
    reason: str


class EvaluationOut(BaseModel):
    id: uuid.UUID
    total: int
    passed: bool
    scores: list[CriterionScoreOut]
    missed_points: list[str]
    advice: str
    rubric_version: str
    prompt_version: str
    model: str
    latency_ms: int
    created_at: datetime


class AnswerOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID | None
    question_id: uuid.UUID
    kind: str
    text: str | None
    status: str
    error: str | None
    created_at: datetime
    evaluation: EvaluationOut | None


class SummaryQuestionOut(BaseModel):
    position: int
    question_id: uuid.UUID
    answer_status: str | None
    total: int | None
    passed: bool | None


class SessionSummaryOut(BaseModel):
    id: uuid.UUID
    status: str
    finished_at: datetime | None
    questions: list[SummaryQuestionOut]
    answered: int
    evaluated: int
    passed: int
    avg_total: float | None
