"""Подробный разбор ответа по кнопке (010 §4): схема ответа модели, починка, промпт.

Статусы ключевых пунктов ставит код по оценке, модель их только комментирует. Ссылки «что почитать»
модель не видит и не пишет — их подставляет API из карты тем.
"""

import re
from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sobesnik_core.evaluation import ANSWER_SOURCES, AnswerKind, format_reference_answer
from sobesnik_core.prompt import PromptTemplate, RenderedPrompt

PointStatus = Literal["covered", "missed", "doubtful"]
"""Пункт прозвучал / не прозвучал / оценщик счёл сам пункт фактически неверным (`wrong_points`)."""

POINT_LABELS: dict[PointStatus, str] = {
    "covered": "прозвучал в ответе",
    "missed": "не прозвучал в ответе",
    "doubtful": "оценщик считает сам пункт фактически неверным",
}

REVIEW_MISTAKES_MAX = 3
REVIEW_REFERENCE_MAX_CHARS = 1_500
REVIEW_COMMENT_MAX_CHARS = 400
REVIEW_QUOTE_MIN_CHARS = 3

_EMPTY_ISSUE = re.compile(r"^(нет|нету|none|null|-+|—|нет неточностей|неточностей нет)\.?$", re.I)


class ReviewPoint(BaseModel):
    n: int
    """Номер ключевого пункта, с 1."""
    comment: str = ""


class ReviewMistake(BaseModel):
    quote: str
    """Дословная цитата из ответа."""
    correction: str


class ReviewOutput(BaseModel):
    """Ответ модели при разборе и одновременно его валидатор."""

    model_config = ConfigDict(extra="forbid")

    question_issue: str = ""
    """Неверное утверждение в самом вопросе (010 §7); пусто — вопрос в порядке."""
    reference: str = Field(min_length=1)
    points: list[ReviewPoint] = []
    mistakes: list[ReviewMistake] = []


def review_json_schema() -> dict[str, Any]:
    """`question_issue` первым: модель сначала смотрит на сам вопрос, потом пишет эталон."""
    text = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "question_issue": text,
            "reference": text,
            "points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}, "comment": text},
                    "required": ["n", "comment"],
                },
            },
            "mistakes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"quote": text, "correction": text},
                    "required": ["quote", "correction"],
                },
            },
        },
        "required": ["question_issue", "reference", "points", "mistakes"],
    }


def normalize_quote(text: str) -> str:
    text = text.lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]", " ", text).split())


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def make_review_repair(
    points_count: int, answer_text: str
) -> Callable[[ReviewOutput], ReviewOutput]:
    """Детерминированная починка разбора.

    Комментарии к пунктам вне диапазона и повторы выбрасываются. Неточность остаётся, только
    если её цитата есть в ответе: модель не приписывает человеку слов, которых он не говорил.
    """
    answer = normalize_quote(answer_text)

    def repair(output: ReviewOutput) -> ReviewOutput:
        points: dict[int, ReviewPoint] = {}
        for point in output.points:
            if 1 <= point.n <= points_count and point.n not in points:
                comment = _clip(point.comment, REVIEW_COMMENT_MAX_CHARS)
                points[point.n] = ReviewPoint(n=point.n, comment=comment)
        mistakes: list[ReviewMistake] = []
        for mistake in output.mistakes:
            quote = normalize_quote(mistake.quote)
            if len(quote) < REVIEW_QUOTE_MIN_CHARS or quote not in answer:
                continue
            if any(normalize_quote(m.quote) == quote for m in mistakes):
                continue
            mistakes.append(
                ReviewMistake(
                    quote=_clip(mistake.quote.strip(" «»\"'"), REVIEW_COMMENT_MAX_CHARS),
                    correction=_clip(mistake.correction, REVIEW_COMMENT_MAX_CHARS),
                )
            )
        issue = _clip(output.question_issue, REVIEW_COMMENT_MAX_CHARS)
        return ReviewOutput(
            question_issue="" if _EMPTY_ISSUE.match(issue) else issue,
            reference=_clip(output.reference, REVIEW_REFERENCE_MAX_CHARS),
            points=[points[n] for n in sorted(points)],
            mistakes=mistakes[:REVIEW_MISTAKES_MAX],
        )

    return repair


def point_statuses(
    key_points: Sequence[str], missed: Sequence[str], wrong: Sequence[str]
) -> list[PointStatus]:
    """Статусы пунктов по оценке: в ней упущенные и неверные пункты хранятся текстами."""
    statuses: list[PointStatus] = []
    for point in key_points:
        if point in wrong:
            statuses.append("doubtful")
        elif point in missed:
            statuses.append("missed")
        else:
            statuses.append("covered")
    return statuses


def format_review_points(key_points: Sequence[str], statuses: Sequence[PointStatus]) -> str:
    if not key_points:
        return "(не заданы — блок points оставь пустым)"
    return "\n".join(
        f"{i}. [{POINT_LABELS[status]}] {point}"
        for i, (point, status) in enumerate(zip(key_points, statuses, strict=True), start=1)
    )


def review_prompt(
    template: PromptTemplate,
    question: str,
    key_points: Sequence[str],
    statuses: Sequence[PointStatus],
    answer: str,
    advice: str,
    kind: AnswerKind = "text",
    reference_answer: str | None = None,
) -> RenderedPrompt:
    return template.render(
        answer_source=ANSWER_SOURCES[kind],
        question=question,
        key_points=format_review_points(key_points, statuses),
        reference_answer=format_reference_answer(reference_answer),
        advice=advice.strip() or "(нет)",
        answer=answer.strip().replace("</answer>", "").replace("<answer>", ""),
    )
