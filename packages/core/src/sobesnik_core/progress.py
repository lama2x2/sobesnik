"""Прогресс по темам и выбор тем для повторения (PRG-1…3, 007 §4–5, SM-2 — 011 §5)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sobesnik_core.repetition import Schedule, schedule

TopicStatus = Literal["weak", "few", "ok"]

LAST_N = 3
"""Сколько последних попыток усредняется (PRG-1)."""


@dataclass(frozen=True)
class Thresholds:
    min_attempts: int = 2
    """Меньше попыток — «мало данных»: одна оценка шумит на ±20 (007 §5)."""
    weak_avg: float = 60
    """Среднее последних попыток ниже — тема слабая."""
    low_score: float = 60
    """«Мало данных» с баллом ниже — «похоже, стоит повторить», такие темы берёт `/repeat`."""


@dataclass(frozen=True)
class TopicStats:
    attempts: int
    last_scores: tuple[int, ...]
    """До `LAST_N` последних баллов, новые первыми."""
    schedule: Schedule | None = None
    """Срок повторения SM-2; `None` — ещё не посчитан (до пересчёта), считается «сейчас»."""

    def due(self, now: datetime) -> bool:
        return self.schedule is None or self.schedule.due(now)

    @property
    def last_score(self) -> int | None:
        return self.last_scores[0] if self.last_scores else None

    @property
    def avg_last3(self) -> float | None:
        if not self.last_scores:
            return None
        return sum(self.last_scores) / len(self.last_scores)


def topic_stats(scores_newest_first: Sequence[int], attempts: int | None = None) -> TopicStats:
    """Статистика темы по баллам попыток (новые первыми). `attempts` — если баллы уже обрезаны."""
    return TopicStats(
        attempts=len(scores_newest_first) if attempts is None else attempts,
        last_scores=tuple(scores_newest_first[:LAST_N]),
    )


def stats_from_history(history_newest_first: Sequence[tuple[int, datetime]]) -> TopicStats:
    """Статистика и срок SM-2 по всем попыткам темы `(балл, время)`, новые первыми."""
    return TopicStats(
        attempts=len(history_newest_first),
        last_scores=tuple(score for score, _ in history_newest_first[:LAST_N]),
        schedule=schedule(list(reversed(history_newest_first))),
    )


def topic_status(stats: TopicStats, thresholds: Thresholds) -> TopicStatus:
    """Статус темы: последний балл сам по себе тему слабой не делает (007 §14 п. 2)."""
    avg = stats.avg_last3
    if stats.attempts < thresholds.min_attempts or avg is None:
        return "few"
    return "weak" if avg < thresholds.weak_avg else "ok"


def is_low(stats: TopicStats, thresholds: Thresholds) -> bool:
    """«Мало данных», но балл низкий — «похоже, стоит повторить»."""
    avg = stats.avg_last3
    return (
        topic_status(stats, thresholds) == "few" and avg is not None and avg < thresholds.low_score
    )


def sort_key(stats: TopicStats, thresholds: Thresholds) -> tuple[int, float]:
    """Порядок в прогрессе: слабые, потом «мало данных», потом «в порядке»; внутри — по среднему."""
    order = {"weak": 0, "few": 1, "ok": 2}[topic_status(stats, thresholds)]
    return order, stats.avg_last3 if stats.avg_last3 is not None else 0.0


def repeat_topics(
    progress: dict[str, TopicStats],
    thresholds: Thresholds,
    now: datetime,
    limit: int,
    *,
    force: bool = False,
) -> list[str]:
    """Темы для повторения (PRG-3, 011 §5): со сроком SM-2 не позже `now` — слабые, потом «мало
    данных», потом «в порядке», внутри — от самых низких.

    Сроков нет и `force` — темы с ближайшим сроком («Всё равно повторить»)."""

    def by_status(slug: str) -> tuple[int, float, str]:
        return (*sort_key(progress[slug], thresholds), slug)

    due = sorted((s for s, st in progress.items() if st.due(now)), key=by_status)
    if due or not force:
        return due[:limit]

    def by_due(slug: str) -> tuple[datetime, int, float, str]:
        sched = progress[slug].schedule
        assert sched is not None  # без срока тема была бы в `due`
        return (sched.due_at, *by_status(slug))

    return sorted(progress, key=by_due)[:limit]


def next_due(progress: dict[str, TopicStats]) -> tuple[str, datetime] | None:
    """Тема с ближайшим сроком повторения."""
    dated = [(st.schedule.due_at, s) for s, st in progress.items() if st.schedule is not None]
    if not dated:
        return None
    due_at, slug = min(dated)
    return slug, due_at
