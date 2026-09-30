"""Вопросы, которые модель генерирует по профилю вакансии."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GeneratedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    requirement_index: int | None = Field(
        default=None, description="Номер требования из списка вакансии, начиная с 0"
    )


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
