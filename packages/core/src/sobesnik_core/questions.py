"""Вопросы, которые модель генерирует по плану (002 §9)."""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sobesnik_core.limits import KEY_POINTS_MAX, KEY_POINTS_MIN

KeyPoint = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class GeneratedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    key_points: list[KeyPoint] = Field(min_length=KEY_POINTS_MIN, max_length=KEY_POINTS_MAX)
    """Проверяемые утверждения, которые должны прозвучать в хорошем ответе."""


class GeneratedQuestions(BaseModel):
    """Схема ответа модели при генерации вопросов и одновременно её валидатор."""

    model_config = ConfigDict(extra="forbid")

    questions: list[GeneratedQuestion] = Field(min_length=1)


def questions_json_schema(n: int) -> dict[str, Any]:
    """JSON Schema ответа ровно с `n` вопросами — для structured output провайдера."""
    schema = GeneratedQuestions.model_json_schema()
    schema["properties"]["questions"]["minItems"] = n
    schema["properties"]["questions"]["maxItems"] = n
    return schema
