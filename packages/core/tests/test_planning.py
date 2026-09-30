import pytest

from sobesnik_core.planning import PlanRequirement, PlanSlot, plan_questions


def req(required: bool, topic: str | None, asked: int = 0) -> PlanRequirement:
    return PlanRequirement(is_required=required, topic=topic, asked=asked)


def indexes(slots: list[PlanSlot]) -> list[int]:
    return [s.requirement_index for s in slots]


def test_required_first() -> None:
    reqs = [req(False, "a"), req(True, "b"), req(True, "c"), req(False, "d")]
    assert indexes(plan_questions(reqs, 3)) == [1, 2, 0]


def test_topic_diversity() -> None:
    reqs = [req(True, "db"), req(True, "db"), req(True, "go"), req(False, "k8s")]
    # второе требование по db откладывается, пока есть другие темы
    assert indexes(plan_questions(reqs, 3)) == [0, 2, 3]
    assert indexes(plan_questions(reqs, 4)) == [0, 2, 3, 1]


def test_none_topic_never_deferred() -> None:
    reqs = [req(True, None), req(True, None), req(True, "x")]
    assert indexes(plan_questions(reqs, 3)) == [0, 1, 2]


def test_more_slots_than_requirements() -> None:
    reqs = [req(True, "a"), req(False, "b")]
    slots = plan_questions(reqs, 5)
    assert slots == [
        PlanSlot(0, 1),
        PlanSlot(1, 1),
        PlanSlot(0, 2),
        PlanSlot(1, 2),
        PlanSlot(0, 3),
    ]


def test_unasked_requirements_first() -> None:
    reqs = [req(True, "a", asked=2), req(True, "b", asked=1), req(False, "c"), req(True, "d")]
    assert indexes(plan_questions(reqs, 4)) == [3, 2, 1, 0]


def test_deterministic() -> None:
    reqs = [req(i % 2 == 0, f"t{i % 3}", asked=i % 2) for i in range(8)]
    assert plan_questions(reqs, 15) == plan_questions(reqs, 15)
    assert len(plan_questions(reqs, 15)) == 15


def test_empty() -> None:
    with pytest.raises(ValueError):
        plan_questions([], 3)
