"""Оценка ответа по рубрике (003 §6, 004 §8.6, 005 §10): схема ответа модели, починка, проверка,
промпт, итог."""

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sobesnik_core.prompt import PromptTemplate, RenderedPrompt
from sobesnik_core.rubric import LEVELS, Rubric

log = logging.getLogger(__name__)

Score = Literal[0, 1, 2, 3]
AnswerKind = Literal["text", "voice"]

ASR_DOUBTS_MAX = 5
ASR_DOUBT_MAX_CHARS = 200
ANSWER_SOURCES: dict[AnswerKind, str] = {
    "text": "письменный ответ",
    "voice": "автоматическая расшифровка устного ответа (распознавание речи)",
}


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
    asr_doubts: list[str] = []
    """Места, где мог ошибиться распознаватель речи. Только у голосовых ответов (004 §8.6)."""


def evaluation_json_schema(rubric: Rubric, kind: AnswerKind = "text") -> dict[str, Any]:
    """JSON Schema для structured output: поле на каждый критерий, `reason` до `score`.

    `asr_doubts` есть только у голосового ответа: у текстового модель не может его выдумать.
    """
    criterion = {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "score": {"type": "integer", "enum": list(LEVELS)},
        },
        "required": ["reason", "score"],
    }
    points = {"type": "array", "items": {"type": "integer"}}
    schema: dict[str, Any] = {
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
    if kind == "voice":
        # первым полем: модель пишет JSON по порядку и сначала просматривает расшифровку на огрехи
        doubts = {"type": "array", "items": {"type": "string"}}
        schema["properties"] = {"asr_doubts": doubts, **schema["properties"]}
        schema["required"] = ["asr_doubts", *schema["required"]]
    return schema


def _clean_points(numbers: Sequence[int], count: int) -> list[int]:
    return sorted({n for n in numbers if 1 <= n <= count})


_RECOGNIZED_TYPO = re.compile(r"^распи[зс]нано", re.IGNORECASE)
"""Маленькая модель путает «распознано» с похожим словом — пользователь это видит (005 «Итоги»)."""


def _clean_doubts(doubts: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    for doubt in doubts:
        text = _RECOGNIZED_TYPO.sub("распознано", " ".join(doubt.split()))[:ASR_DOUBT_MAX_CHARS]
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:ASR_DOUBTS_MAX]


def make_repair(
    points_count: int, kind: AnswerKind = "text"
) -> Callable[[EvaluationOutput], EvaluationOutput]:
    """Детерминированная починка: номера пунктов вне диапазона и дубли выбрасываются,
    неверные пункты не считаются упущенными; сомнения в распознавании чистятся и обрезаются,
    у текстового ответа их нет."""

    def repair(output: EvaluationOutput) -> EvaluationOutput:
        wrong = _clean_points(output.wrong_points, points_count)
        missed = [n for n in _clean_points(output.missed_points, points_count) if n not in wrong]
        dropped = len(output.missed_points) + len(output.wrong_points) - len(missed) - len(wrong)
        if dropped:
            log.info("evaluation points repaired", extra={"dropped_points": dropped})
        doubts = _clean_doubts(output.asr_doubts) if kind == "voice" else []
        return output.model_copy(
            update={"missed_points": missed, "wrong_points": wrong, "asr_doubts": doubts}
        )

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


def format_reference_answer(reference_answer: str | None) -> str:
    if not reference_answer or not reference_answer.strip():
        return ""
    return (
        "Эталонный ответ (ориентир для полноты и корректности, не единственно верная "
        f"формулировка):\n{reference_answer.strip()}\n"
    )


def _strip_tags(text: str) -> str:
    """Ответ не может закрыть блок <answer> раньше времени."""
    return text.replace("</answer>", "").replace("<answer>", "")


def evaluation_prompt(
    template: PromptTemplate,
    rubric: Rubric,
    question: str,
    key_points: Sequence[str],
    answer: str,
    kind: AnswerKind = "text",
    reference_answer: str | None = None,
) -> RenderedPrompt:
    """`$answer_source` нужен промпту v2 и выше, `$reference_answer` — v3 и выше;
    старые версии их не используют."""
    return template.render(
        answer_source=ANSWER_SOURCES[kind],
        rubric=rubric.render(),
        question=question,
        key_points=format_key_points(key_points),
        reference_answer=format_reference_answer(reference_answer),
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
    asr_doubts: list[str] = field(default_factory=list)


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
        asr_doubts=list(output.asr_doubts),
    )
