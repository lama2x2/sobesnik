"""Проверка ключевых пунктов вопросов сессии в фоне (011 §6): один вызов модели на сессию.

Сбой проверки ничего не ломает: у вопроса остаётся `key_points_check = null`, оценка и разбор
работают как раньше. Тексты пунктов и сомнений в лог не пишутся — только число сомнений.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Question, TrainingSession
from sobesnik_core.key_points_check import (
    KeyPointsCheckOutput,
    check_prompt,
    issues_by_question,
    key_points_check_json_schema,
    make_check_repair,
)
from sobesnik_core.limits import KEY_POINTS_CHECK_MAX_TOKENS
from sobesnik_core.prompt import load_prompt
from sobesnik_llm import LLMError, LLMProvider, LLMRequest, generate_structured

log = logging.getLogger(__name__)

CHECK_PROMPT = "check_key_points"


@dataclass(frozen=True)
class CheckResult:
    issues: list[list[dict[str, Any]]]
    """Сомнения по вопросам, в порядке вопросов: `[{"n": 2, "issue": "…"}]`."""
    model: str
    prompt_version: str
    latency_ms: int


async def check_questions(
    llm: LLMProvider, questions: Sequence[tuple[str, Sequence[str]]], *, max_retries: int = 2
) -> CheckResult:
    """Один вызов модели на все вопросы `(текст, пункты)`. Ошибки LLM пробрасываются."""
    template = load_prompt(CHECK_PROMPT)
    rendered = check_prompt(template, questions)
    result = await generate_structured(
        llm,
        LLMRequest(
            system=rendered.system,
            prompt=rendered.user,
            temperature=template.temperature,
            seed=template.seed,
            max_tokens=KEY_POINTS_CHECK_MAX_TOKENS,
        ),
        KeyPointsCheckOutput,
        json_schema=key_points_check_json_schema(),
        repair=make_check_repair([len(points) for _, points in questions]),
        max_retries=max_retries,
    )
    return CheckResult(
        issues=issues_by_question(result.value, len(questions)),
        model=result.attempts[-1].model,
        prompt_version=template.version,
        latency_ms=sum(a.latency_ms for a in result.attempts),
    )


async def check_session(
    db: AsyncSession, llm: LLMProvider, session_id: uuid.UUID, *, max_retries: int = 2
) -> int | None:
    """Проверяет пункты ещё не проверенных вопросов сессии. Возвращает число сомнений;
    `None` — проверять нечего или модель не ответила."""
    session = await db.get(TrainingSession, session_id)
    if session is None:
        return None
    pending: list[Question] = [
        item.question
        for item in session.items
        if item.question.key_points and item.question.key_points_check is None
    ]
    if not pending:
        return None
    try:
        result = await check_questions(
            llm, [(q.text, q.key_points) for q in pending], max_retries=max_retries
        )
    except LLMError as exc:
        log.warning(
            "key points not checked",
            extra={"session_id": str(session_id), "error": type(exc).__name__},
        )
        return None
    for question, issues in zip(pending, result.issues, strict=True):
        question.key_points_check = {
            "issues": issues,
            "model": result.model,
            "prompt_version": result.prompt_version,
            "latency_ms": result.latency_ms,
        }
    await db.commit()
    doubts = sum(len(issues) for issues in result.issues)
    log.info(
        "key points checked",
        extra={
            "session_id": str(session_id),
            "questions": len(pending),
            "doubts": doubts,
            "llm_model": result.model,
            "latency_ms": result.latency_ms,
        },
    )
    return doubts
