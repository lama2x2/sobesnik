"""Оценка ответа по рубрике (003 §6, §8.2): вызов модели без БД и запись оценки."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Answer, Evaluation
from sobesnik_core.evaluation import (
    EvaluationOutput,
    ScoredEvaluation,
    evaluation_json_schema,
    evaluation_prompt,
    make_check,
    make_repair,
    score_evaluation,
)
from sobesnik_core.limits import EVAL_MAX_TOKENS
from sobesnik_core.prompt import PromptTemplate, load_prompt
from sobesnik_core.rubric import Rubric, load_rubric
from sobesnik_llm import (
    LLMError,
    LLMOutputError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
    generate_structured,
)

log = logging.getLogger(__name__)

EVAL_PROMPT = "evaluate_answer"


@dataclass(frozen=True)
class EvalConfig:
    """Рубрика, промпт и параметры, с которыми делается оценка."""

    rubric: Rubric
    template: PromptTemplate
    max_retries: int = 2
    num_ctx: int | None = None

    @classmethod
    def latest(cls, *, max_retries: int = 2, num_ctx: int | None = None) -> "EvalConfig":
        return cls(load_rubric(), load_prompt(EVAL_PROMPT), max_retries, num_ctx)

    @property
    def params(self) -> dict[str, Any]:
        return {
            "temperature": self.template.temperature,
            "seed": self.template.seed,
            "max_tokens": EVAL_MAX_TOKENS,
            "num_ctx": self.num_ctx,
        }


@dataclass(frozen=True)
class EvaluationResult:
    scored: ScoredEvaluation
    attempts: list[LLMResponse]


async def evaluate_answer(
    llm: LLMProvider,
    config: EvalConfig,
    question: str,
    key_points: Sequence[str],
    answer_text: str,
) -> EvaluationResult:
    """Оценка одного ответа без БД. Ошибки LLM пробрасываются."""
    rendered = evaluation_prompt(config.template, config.rubric, question, key_points, answer_text)
    result = await generate_structured(
        llm,
        LLMRequest(
            system=rendered.system,
            prompt=rendered.user,
            temperature=config.template.temperature,
            seed=config.template.seed,
            max_tokens=EVAL_MAX_TOKENS,
        ),
        EvaluationOutput,
        json_schema=evaluation_json_schema(config.rubric),
        repair=make_repair(len(key_points)),
        check=make_check(config.rubric),
        max_retries=config.max_retries,
    )
    scored = score_evaluation(config.rubric, result.value, key_points)
    return EvaluationResult(scored=scored, attempts=result.attempts)


def _sum(values: Sequence[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _evaluation_row(
    llm: LLMProvider,
    config: EvalConfig,
    answer_id: uuid.UUID,
    run_id: uuid.UUID | None,
    attempts: Sequence[LLMResponse],
) -> Evaluation:
    last = attempts[-1] if attempts else None
    return Evaluation(
        answer_id=answer_id,
        eval_run_id=run_id,
        rubric_version=config.rubric.version,
        prompt_version=config.template.version,
        provider=last.provider if last else llm.name,
        model=last.model if last else llm.model,
        params=config.params,
        raw_response=last.text if last else None,
        attempts=len(attempts),
        latency_ms=sum(a.latency_ms for a in attempts),
        tokens_in=_sum([a.tokens_in for a in attempts]),
        tokens_out=_sum([a.tokens_out for a in attempts]),
    )


async def evaluate_and_store(
    db: AsyncSession,
    llm: LLMProvider,
    config: EvalConfig,
    answer: Answer,
    *,
    run_id: uuid.UUID | None = None,
) -> Evaluation:
    """Оценивает ответ и добавляет оценку в сессию БД (без коммита).

    Невалидный ответ модели и прочие ошибки LLM дают оценку со статусом `failed`.
    `LLMUnavailableError` пробрасывается: что делать, решает вызывающий (повтор или сбой прогона).
    """
    question = answer.question
    try:
        result = await evaluate_answer(
            llm, config, question.text, question.key_points, answer.text or ""
        )
    except LLMUnavailableError:
        raise
    except LLMOutputError as exc:
        row = _evaluation_row(llm, config, answer.id, run_id, exc.responses)
        row.status, row.error = "failed", "llm_bad_output"
    except LLMError:
        row = _evaluation_row(llm, config, answer.id, run_id, [])
        row.status, row.error = "failed", "llm_error"
    else:
        scored = result.scored
        row = _evaluation_row(llm, config, answer.id, run_id, result.attempts)
        row.status = "done"
        row.scores = scored.scores
        row.total = scored.total
        row.passed = scored.passed
        row.feedback = {
            "missed_points": scored.missed_points,
            "wrong_points": scored.wrong_points,
            "advice": scored.advice,
        }
    db.add(row)
    log.info(
        "answer evaluated",
        extra={
            "answer_id": str(answer.id),
            "eval_run_id": str(run_id) if run_id else None,
            "status": row.status,
            "error": row.error,
            "attempts": row.attempts,
            "llm_model": row.model,
            "latency_ms": row.latency_ms,
            "tokens_in": row.tokens_in,
            "tokens_out": row.tokens_out,
        },
    )
    return row


async def live_evaluation(db: AsyncSession, answer_id: uuid.UUID) -> Evaluation | None:
    """«Боевая» оценка: последняя успешная оценка вне прогонов (003 §7)."""
    result = await db.scalars(
        select(Evaluation)
        .where(
            Evaluation.answer_id == answer_id,
            Evaluation.status == "done",
            Evaluation.eval_run_id.is_(None),
        )
        .order_by(Evaluation.created_at.desc(), Evaluation.id)
        .limit(1)
    )
    return result.first()
