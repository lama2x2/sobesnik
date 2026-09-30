"""Структурированный вывод: JSON-схема → вызов → валидация → повтор с описанием ошибок."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from sobesnik_llm.base import LLMOutputError, LLMProvider, LLMRequest, LLMResponse

log = logging.getLogger(__name__)

MAX_FEEDBACK_PROBLEMS = 5

_THINK = re.compile(r"^\s*<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass
class StructuredResult[T: BaseModel]:
    value: T
    attempts: list[LLMResponse]
    """Все ответы модели, включая отвергнутые."""


def clean_json_text(text: str) -> str:
    """Убирает блок рассуждений <think> и обёртку ```json, если модель их добавила."""
    text = _THINK.sub("", text, count=1)
    match = _FENCE.match(text)
    return match.group(1) if match else text.strip()


def _feedback(problems: list[str]) -> str:
    items = "\n".join(f"- {p}" for p in problems[:MAX_FEEDBACK_PROBLEMS])
    return f"\n\nПредыдущий ответ не прошёл проверку:\n{items}\nВерни исправленный JSON целиком."


def _validation_problems(exc: ValidationError) -> list[str]:
    problems = []
    for error in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(part) for part in error["loc"])
        problems.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    return problems


async def generate_structured[T: BaseModel](
    llm: LLMProvider,
    request: LLMRequest,
    schema: type[T],
    *,
    json_schema: dict[str, Any] | None = None,
    check: Callable[[T], list[str]] | None = None,
    max_retries: int = 2,
) -> StructuredResult[T]:
    """Вызывает модель, пока ответ не пройдёт схему и `check`, но не больше `1 + max_retries` раз.

    `check` возвращает список проблем; пустой список — ответ принят. Недоступность провайдера
    и прочие ошибки LLM не повторяются. Тексты промптов и ответов в лог не пишутся.
    """
    base = request.model_copy(update={"json_schema": json_schema or schema.model_json_schema()})
    attempts: list[LLMResponse] = []
    problems: list[str] = []
    for attempt in range(max_retries + 1):
        current = base
        if attempt:
            seed = base.seed + attempt if base.seed is not None else None
            current = base.model_copy(
                update={"prompt": base.prompt + _feedback(problems), "seed": seed}
            )
        response = await llm.generate(current)
        attempts.append(response)
        try:
            value = schema.model_validate_json(clean_json_text(response.text))
        except ValidationError as exc:
            problems = _validation_problems(exc)
            error_types = sorted({e["type"] for e in exc.errors(include_input=False)})
        else:
            problems = check(value) if check else []
            error_types = ["check"] if problems else []
        log.info(
            "llm attempt",
            extra={
                "schema": schema.__name__,
                "attempt": attempt + 1,
                "accepted": not problems,
                "error_types": error_types,
                "problems": len(problems),
                "llm_model": response.model,
                "latency_ms": response.latency_ms,
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
            },
        )
        if not problems:
            return StructuredResult(value=value, attempts=attempts)
    raise LLMOutputError(problems, attempts=len(attempts))
