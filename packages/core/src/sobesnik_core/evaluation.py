"""Оценка ответа по рубрике (003 §6): схема ответа модели, починка, проверка, промпт, итог."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sobesnik_core.prompt import PromptTemplate, RenderedPrompt
from sobesnik_core.rubric import LEVELS, Rubric

log = logging.getLogger(__name__)

Score = Literal[0, 1, 2, 3]


class CriterionScore(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    """Обоснование: модель пишет его до балла."""
    score: Score


class EvaluationOutput(BaseModel):
    """Ответ модели при оценке и одновременно его валидатор."""

    model_config = ConfigDict(extra="forbid")

    scores: dict[str, CriterionScore]
    missed_points: list[int]
    """Номера ключевых пунктов (с 1), которые в ответе не прозвучали."""
    wrong_points: list[int]
    """Номера ключевых пунктов, которые оценщик считает фактически неверными."""
    advice: str = Field(min_length=1, max_length=600)


def evaluation_json_schema(rubric: Rubric) -> dict[str, Any]:
    """JSON Schema для structured output: поле на каждый критерий, `reason` до `score`."""
    criterion = {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "score": {"type": "integer", "enum": list(LEVELS)},
        },
        "required": ["reason", "score"],
    }
    points = {"type": "array", "items": {"type": "integer"}}
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "properties": {cid: criterion for cid in rubric.ids},
                "required": rubric.ids,
                "additionalProperties": False,
            },
            "missed_points": points,
            "wrong_points": points,
            "advice": {"type": "string"},
        },
        "required": ["scores", "missed_points", "wrong_points", "advice"],
    }


def _clean_points(numbers: Sequence[int], count: int) -> list[int]:
    return sorted({n for n in numbers if 1 <= n <= count})


def make_repair(points_count: int) -> Callable[[EvaluationOutput], EvaluationOutput]:
    """Детерминированная починка номеров пунктов: вне диапазона и дубли выбрасываются,
    неверные пункты не считаются упущенными."""

    def repair(output: EvaluationOutput) -> EvaluationOutput:
        wrong = _clean_points(output.wrong_points, points_count)
        missed = [n for n in _clean_points(output.missed_points, points_count) if n not in wrong]
        dropped = len(output.missed_points) + len(output.wrong_points) - len(missed) - len(wrong)
        if dropped:
            log.info("evaluation points repaired", extra={"dropped_points": dropped})
        return output.model_copy(update={"missed_points": missed, "wrong_points": wrong})

    return repair


def make_check(rubric: Rubric) -> Callable[[EvaluationOutput], list[str]]:
    def check(output: EvaluationOutput) -> list[str]:
        problems = []
        missing = [cid for cid in rubric.ids if cid not in output.scores]
        extra = sorted(set(output.scores) - set(rubric.ids))
        if missing:
            problems.append(f"scores: нет критериев {', '.join(missing)}")
        if extra:
            problems.append(
                f"scores: лишние критерии {', '.join(extra)}; допустимы {', '.join(rubric.ids)}"
            )
        return problems

    return check


def format_key_points(key_points: Sequence[str]) -> str:
    if not key_points:
        return "(не заданы — полноту оцени по своему пониманию правильного ответа)"
    return "\n".join(f"{i}. {point}" for i, point in enumerate(key_points, start=1))


def _strip_tags(text: str) -> str:
    """Ответ не может закрыть блок <answer> раньше времени."""
    return text.replace("</answer>", "").replace("<answer>", "")


def evaluation_prompt(
    template: PromptTemplate,
    rubric: Rubric,
    question: str,
    key_points: Sequence[str],
    answer: str,
) -> RenderedPrompt:
    return template.render(
        rubric=rubric.render(),
        question=question,
        key_points=format_key_points(key_points),
        answer=_strip_tags(answer.strip()),
    )


@dataclass(frozen=True)
class ScoredEvaluation:
    """Ответ модели после расчёта итога кодом."""

    scores: dict[str, dict[str, Any]]
    """{criterion: {"score": 0–3, "reason": "…"}} в порядке рубрики."""
    total: int
    passed: bool
    missed_points: list[str]
    wrong_points: list[str]
    advice: str


def score_evaluation(
    rubric: Rubric, output: EvaluationOutput, key_points: Sequence[str]
) -> ScoredEvaluation:
    numbers = {cid: int(output.scores[cid].score) for cid in rubric.ids}
    return ScoredEvaluation(
        scores={
            cid: {"score": numbers[cid], "reason": output.scores[cid].reason} for cid in rubric.ids
        },
        total=rubric.total(numbers),
        passed=rubric.passed(numbers),
        missed_points=[key_points[n - 1] for n in output.missed_points],
        wrong_points=[key_points[n - 1] for n in output.wrong_points],
        advice=output.advice.strip(),
    )
