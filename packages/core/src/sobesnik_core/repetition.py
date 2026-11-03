"""Интервальное повторение SM-2 по истории попыток темы (011 §5).

SM-2 (Wozniak, 1990) с двумя поправками: балл 0–100 переводится в качество 0–5 с порогом «сдал» 60 —
тем же, что у слабой темы (PRG-2); провал ставит срок «сейчас», а не через день, чтобы слабая тема
сразу шла в повторение. Состояние не накапливается, а проигрывается из истории заново: повтор задачи
или пересчёт прогресса дают тот же результат.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

EASE_START = 2.5
EASE_MIN = 1.3
FIRST_INTERVALS = (1, 6)
"""Интервалы в днях после первого и второго успешного повторения подряд."""

QUALITY_FROM = ((90, 5), (75, 4), (60, 3), (40, 2), (20, 1))
"""Нижняя граница балла → качество ответа SM-2; ниже 20 — 0."""
PASS_QUALITY = 3


def quality(score: int) -> int:
    return next((q for bound, q in QUALITY_FROM if score >= bound), 0)


@dataclass(frozen=True)
class Schedule:
    reps: int
    """Успешных повторений подряд."""
    ease: float
    interval_days: int
    due_at: datetime
    """Когда повторить: последняя попытка + интервал."""

    def due(self, now: datetime) -> bool:
        return self.due_at <= now


def next_ease(ease: float, q: int) -> float:
    return max(EASE_MIN, ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))


def schedule(attempts: Sequence[tuple[int, datetime]]) -> Schedule | None:
    """Срок повторения по попыткам `(балл, время)` в порядке времени, старые первыми."""
    if not attempts:
        return None
    reps, ease, interval = 0, EASE_START, 0
    for score, _ in attempts:
        q = quality(score)
        if q < PASS_QUALITY:
            reps, interval = 0, 0
        else:
            if reps < len(FIRST_INTERVALS):
                interval = FIRST_INTERVALS[reps]
            else:
                interval = round(interval * ease)
            reps += 1
        ease = next_ease(ease, q)
    last_at = attempts[-1][1]
    return Schedule(
        reps=reps,
        ease=round(ease, 4),
        interval_days=interval,
        due_at=last_at + timedelta(days=interval),
    )


def due_in(due_at: datetime, now: datetime) -> str:
    """Когда повторить, без часового пояса: «сейчас», «через 5 ч», «через сутки», «через 6 дн.»."""
    left = due_at - now
    if left <= timedelta(0):
        return "сейчас"
    hours = max(1, round(left.total_seconds() / 3600))
    if hours < 24:
        return f"через {hours} ч"
    days = round(hours / 24)
    return "через сутки" if days == 1 else f"через {days} дн."
