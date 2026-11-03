"""Проверка ключевых пунктов эталона отдельным вызовом модели (011 §6).

Пункты пишет та же модель, что и вопросы, и иногда ошибается. Неверный пункт снимает баллы с
верного ответа, поэтому все пункты вопросов сессии проверяются одним вызовом в фоне. Отдельный
вызов, а не поле оценки или разбора: в одном ответе маленькая модель путает ошибки кандидата и
ошибки эталона (analysis §2.6).
"""

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from sobesnik_core.prompt import PromptTemplate, RenderedPrompt

ISSUE_MAX_CHARS = 300
ISSUE_MIN_CHARS = 5


class KeyPointIssue(BaseModel):
    q: int
    """Номер вопроса, с 1."""
    n: int
    """Номер пункта в вопросе, с 1."""
    issue: str
    """Что в пункте неверно и как правильно."""


class KeyPointsCheckOutput(BaseModel):
    """Ответ модели и его валидатор: только сомнительные пункты, обычно пусто."""

    model_config = ConfigDict(extra="forbid")

    issues: list[KeyPointIssue] = []


def key_points_check_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "q": {"type": "integer"},
                        "n": {"type": "integer"},
                        "issue": {"type": "string"},
                    },
                    "required": ["q", "n", "issue"],
                },
            }
        },
        "required": ["issues"],
    }


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def make_check_repair(
    points_per_question: Sequence[int],
) -> Callable[[KeyPointsCheckOutput], KeyPointsCheckOutput]:
    """Пункты вне диапазона, повторы и пустые объяснения выбрасываются."""

    def repair(output: KeyPointsCheckOutput) -> KeyPointsCheckOutput:
        seen: set[tuple[int, int]] = set()
        issues = []
        for item in output.issues:
            key = (item.q, item.n)
            if not 1 <= item.q <= len(points_per_question) or key in seen:
                continue
            if not 1 <= item.n <= points_per_question[item.q - 1]:
                continue
            issue = _clip(item.issue, ISSUE_MAX_CHARS)
            if len(issue) < ISSUE_MIN_CHARS:
                continue
            seen.add(key)
            issues.append(KeyPointIssue(q=item.q, n=item.n, issue=issue))
        return KeyPointsCheckOutput(issues=sorted(issues, key=lambda i: (i.q, i.n)))

    return repair


def format_questions(questions: Sequence[tuple[str, Sequence[str]]]) -> str:
    blocks = []
    for q, (text, points) in enumerate(questions, start=1):
        lines = [f"Вопрос {q}: {text}"]
        lines += [f"  {n}. {point}" for n, point in enumerate(points, start=1)]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def check_prompt(
    template: PromptTemplate, questions: Sequence[tuple[str, Sequence[str]]]
) -> RenderedPrompt:
    return template.render(questions=format_questions(questions))


def issues_by_question(output: KeyPointsCheckOutput, count: int) -> list[list[dict[str, Any]]]:
    """Сомнения по вопросам: `[[{"n": 2, "issue": "…"}], [], …]` — в том же порядке, что вопросы."""
    result: list[list[dict[str, Any]]] = [[] for _ in range(count)]
    for item in output.issues:
        result[item.q - 1].append({"n": item.n, "issue": item.issue})
    return result
