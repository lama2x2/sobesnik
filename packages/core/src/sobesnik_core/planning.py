"""План сессии: какое требование проверяет каждый вопрос (GEN-1, 002 §9.1)."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanRequirement:
    is_required: bool
    topic: str | None
    asked: int = 0
    """Сколько вопросов по требованию уже задано в прошлых сессиях вакансии."""


@dataclass(frozen=True)
class PlanSlot:
    requirement_index: int
    aspect: int
    """Номер вопроса по этому требованию в сессии: 1, 2, …"""


def plan_questions(requirements: Sequence[PlanRequirement], n: int) -> list[PlanSlot]:
    """Детерминированный план из `n` слотов.

    Порядок: реже спрошенные раньше, обязательные раньше желательных, дальше порядок в вакансии.
    В каждом круге сначала берутся требования с ещё не занятой в круге темой, потом остальные.
    Если слотов больше, чем требований, круг повторяется.
    """
    if not requirements:
        raise ValueError("нет требований для плана")
    order = sorted(
        range(len(requirements)),
        key=lambda i: (requirements[i].asked, not requirements[i].is_required, i),
    )
    slots: list[PlanSlot] = []
    counts: Counter[int] = Counter()
    while len(slots) < n:
        used: set[str] = set()
        fresh: list[int] = []
        deferred: list[int] = []
        for i in order:
            topic = requirements[i].topic
            if topic is not None and topic in used:
                deferred.append(i)
            else:
                fresh.append(i)
                if topic is not None:
                    used.add(topic)
        for i in fresh + deferred:
            if len(slots) == n:
                break
            counts[i] += 1
            slots.append(PlanSlot(requirement_index=i, aspect=counts[i]))
    return slots
