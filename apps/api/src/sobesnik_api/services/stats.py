"""Сводка времени операций по БД вместо мониторинга (011 §7): медиана, p90, разброс.

Только пользовательские операции — прогоны датасетов и их ответы не входят. Время — модели
(или whisper) на все попытки, без очереди и сети; для замеров 000 §5 этого достаточно.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import (
    Answer,
    AnswerReview,
    Audio,
    Evaluation,
    SessionQuestion,
    TrainingSession,
    Transcript,
    Vacancy,
)


@dataclass(frozen=True)
class Summary:
    name: str
    count: int
    median_s: float | None
    p90_s: float | None
    min_s: float | None
    max_s: float | None


def percentile(values: Sequence[float], q: float) -> float:
    """Перцентиль ближайшего ранга: на десятке замеров честнее интерполяции."""
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def summarize(name: str, values_s: Sequence[float]) -> Summary:
    if not values_s:
        return Summary(name, 0, None, None, None, None)
    ordered = sorted(values_s)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return Summary(
        name=name,
        count=len(ordered),
        median_s=round(median, 1),
        p90_s=round(percentile(ordered, 0.9), 1),
        min_s=round(ordered[0], 1),
        max_s=round(ordered[-1], 1),
    )


async def _seconds(db: AsyncSession, query: Select[Any]) -> list[float]:
    return [float(v) / 1000 for v in (await db.scalars(query)) if v is not None]


async def latency_stats(db: AsyncSession, since: date | None = None) -> list[Summary]:
    start = datetime.combine(since or date.min, time.min, tzinfo=UTC)
    sessions_n = (
        select(SessionQuestion.session_id, func.count().label("n"))
        .group_by(SessionQuestion.session_id)
        .subquery()
    )
    user_answer = (Answer.source == "user", Answer.created_at >= start)
    rows = {
        "разбор вакансии": await _seconds(
            db, select(Vacancy.latency_ms).where(Vacancy.created_at >= start)
        ),
        "генерация вопросов, сессия": await _seconds(
            db,
            select(TrainingSession.gen_latency_ms).where(TrainingSession.created_at >= start),
        ),
        "генерация, на 1 вопрос": await _seconds(
            db,
            select(TrainingSession.gen_latency_ms / sessions_n.c.n)
            .join(sessions_n, sessions_n.c.session_id == TrainingSession.id)
            .where(TrainingSession.created_at >= start),
        ),
        "распознавание, на 1 мин речи": await _seconds(
            db,
            select(Transcript.latency_ms * 60 / func.nullif(Audio.duration_s, 0))
            .join(Audio, Audio.id == Transcript.audio_id)
            .join(Answer, Answer.id == Audio.answer_id)
            .where(Transcript.kind == "asr", *user_answer),
        ),
        "оценка ответа": await _seconds(
            db,
            select(Evaluation.latency_ms)
            .join(Answer, Answer.id == Evaluation.answer_id)
            .where(Evaluation.eval_run_id.is_(None), Evaluation.status == "done", *user_answer),
        ),
        "подробный разбор": await _seconds(
            db,
            select(AnswerReview.latency_ms)
            .join(Answer, Answer.id == AnswerReview.answer_id)
            .where(*user_answer),
        ),
    }
    return [summarize(name, values) for name, values in rows.items()]


def _cell(value: float | None) -> str:
    return "—" if value is None else f"{value:g}".replace(".", ",")


def format_table(summaries: Sequence[Summary]) -> str:
    lines = [
        "| Операция | Замеров | Медиана, с | p90, с | Мин–макс, с |",
        "|---|---|---|---|---|",
    ]
    for s in summaries:
        spread = "—" if s.min_s is None else f"{_cell(s.min_s)}–{_cell(s.max_s)}"
        lines.append(
            f"| {s.name} | {s.count} | {_cell(s.median_s)} | {_cell(s.p90_s)} | {spread} |"
        )
    return "\n".join(lines)
