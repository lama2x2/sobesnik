"""Выгрузка оценок в CSV (EVAL-7, 003 §9.2): широкий формат — строка на оценку,
длинный — строка на пару «оценка × критерий»."""

import csv
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, TextIO

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sobesnik_api.db.models import Answer, EvalRun, Evaluation, Question
from sobesnik_core.rubric import Criterion, load_rubric
from sobesnik_core.versioned import version_number

Format = Literal["wide", "long"]
LIST_SEPARATOR = " | "

COMMON = [
    "evaluation_id",
    "answer_id",
    "run_id",
    "run_name",
    "status",
    "question_id",
    "topic",
    "question",
    "key_points",
    "answer",
    "reference_answer",
    "human_score",
    "rubric_version",
    "prompt_version",
    "provider",
    "model",
    "temperature",
    "seed",
]
TAIL = [
    "total",
    "passed",
    "missed_points",
    "wrong_points",
    "advice",
    "attempts",
    "latency_ms",
    "tokens_in",
    "tokens_out",
    "error",
    "created_at",
]
LONG_CRITERION = ["criterion", "weight", "score", "reason"]


@dataclass(frozen=True)
class ExportFilter:
    run_ids: Sequence[uuid.UUID] = ()
    live: bool = False
    """«Боевые» оценки — без прогона."""
    session_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Row:
    evaluation: Evaluation
    answer: Answer
    question: Question
    run: EvalRun | None


async def select_rows(db: AsyncSession, export: ExportFilter) -> list[Row]:
    sources: list[ColumnElement[bool]] = []
    if export.run_ids:
        sources.append(Evaluation.eval_run_id.in_(export.run_ids))
    if export.live:
        sources.append(Evaluation.eval_run_id.is_(None))
    conds: list[ColumnElement[bool]] = [or_(*sources)] if sources else []
    if export.session_id:
        conds.append(Answer.session_id == export.session_id)
    run = aliased(EvalRun)
    result = await db.execute(
        select(Evaluation, Answer, Question, run)
        .join(Answer, Answer.id == Evaluation.answer_id)
        .join(Question, Question.id == Answer.question_id)
        .outerjoin(run, run.id == Evaluation.eval_run_id)
        .where(and_(*conds))
        .order_by(Answer.created_at, Answer.id, Evaluation.created_at, Evaluation.id)
    )
    return [Row(e, a, q, r) for e, a, q, r in result]


def _criteria(rows: Sequence[Row]) -> list[Criterion]:
    """Критерии всех рубрик, встретившихся в выгрузке, в порядке первой рубрики."""
    seen: dict[str, Criterion] = {}
    for version in dict.fromkeys(r.evaluation.rubric_version for r in rows):
        try:
            rubric = load_rubric(version_number(version))
        except (ValueError, FileNotFoundError):
            continue
        for criterion in rubric.criteria:
            seen.setdefault(criterion.id, criterion)
    for row in rows:  # критерии из оценок, если рубрика не нашлась
        for cid in row.evaluation.scores or {}:
            seen.setdefault(cid, Criterion(cid, cid, 0.0, {}))
    return list(seen.values())


def _join(items: Sequence[Any] | None) -> str:
    return LIST_SEPARATOR.join(str(i) for i in items or [])


def _common(row: Row) -> dict[str, Any]:
    e, a, q = row.evaluation, row.answer, row.question
    feedback = e.feedback or {}
    return {
        "evaluation_id": e.id,
        "answer_id": a.id,
        "run_id": e.eval_run_id or "",
        "run_name": row.run.name if row.run else "",
        "status": e.status,
        "question_id": q.id,
        "topic": q.topic_slug or "",
        "question": q.text,
        "key_points": _join(q.key_points),
        "answer": a.text or "",
        "reference_answer": "",
        "human_score": "",
        "rubric_version": e.rubric_version,
        "prompt_version": e.prompt_version,
        "provider": e.provider,
        "model": e.model,
        "temperature": e.params.get("temperature", ""),
        "seed": e.params.get("seed", ""),
        "total": "" if e.total is None else e.total,
        "passed": "" if e.passed is None else e.passed,
        "missed_points": _join(feedback.get("missed_points")),
        "wrong_points": _join(feedback.get("wrong_points")),
        "advice": feedback.get("advice", ""),
        "attempts": e.attempts,
        "latency_ms": e.latency_ms,
        "tokens_in": "" if e.tokens_in is None else e.tokens_in,
        "tokens_out": "" if e.tokens_out is None else e.tokens_out,
        "error": e.error or "",
        "created_at": e.created_at.isoformat(),
    }


def write_csv(out: TextIO, rows: Sequence[Row], fmt: Format = "wide") -> int:
    """Пишет CSV и возвращает число строк данных."""
    criteria = _criteria(rows)
    if fmt == "wide":
        per_criterion = [f"{c.id}_{part}" for c in criteria for part in ("score", "reason")]
        columns = COMMON + per_criterion + TAIL
    else:
        columns = COMMON + LONG_CRITERION + TAIL
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    count = 0
    for row in rows:
        common = _common(row)
        scores = row.evaluation.scores or {}
        if fmt == "wide":
            for c in criteria:
                item = scores.get(c.id) or {}
                common[f"{c.id}_score"] = item.get("score", "")
                common[f"{c.id}_reason"] = item.get("reason", "")
            writer.writerow(common)
            count += 1
            continue
        for c in criteria:  # у failed-оценки строки критериев с пустыми баллами
            item = scores.get(c.id) or {}
            writer.writerow(
                {
                    **common,
                    "criterion": c.id,
                    "weight": c.weight,
                    "score": item.get("score", ""),
                    "reason": item.get("reason", ""),
                }
            )
            count += 1
    return count
