from datetime import UTC, datetime, timedelta

import pytest

from sobesnik_core.repetition import EASE_MIN, EASE_START, due_in, quality, schedule

T0 = datetime(2026, 11, 1, 9, tzinfo=UTC)


def daily(*scores: int) -> list[tuple[int, datetime]]:
    return [(s, T0 + timedelta(days=i)) for i, s in enumerate(scores)]


@pytest.mark.parametrize(
    ("score", "q"),
    [(100, 5), (90, 5), (89, 4), (75, 4), (74, 3), (60, 3), (59, 2), (40, 2), (20, 1), (19, 0)],
)
def test_quality(score: int, q: int) -> None:
    assert quality(score) == q


def test_empty_history() -> None:
    assert schedule([]) is None


@pytest.mark.parametrize(
    ("scores", "reps", "interval"),
    [
        ((80,), 1, 1),
        ((80, 80), 2, 6),
        ((80, 80, 80), 3, 15),  # 6 × 2,5 (лёгкость при q = 4 не меняется)
        ((80, 80, 80, 80), 4, 38),  # 15 × 2,5 = 37,5 → 38
        ((80, 80, 30), 0, 0),  # провал — сразу, счёт сначала
        ((80, 80, 30, 80), 1, 1),
        ((30,), 0, 0),
    ],
)
def test_intervals(scores: tuple[int, ...], reps: int, interval: int) -> None:
    sched = schedule(daily(*scores))
    assert sched is not None
    assert (sched.reps, sched.interval_days) == (reps, interval)
    assert sched.due_at == T0 + timedelta(days=len(scores) - 1 + interval)


def test_ease_changes_like_sm2() -> None:
    perfect = schedule(daily(95))
    assert perfect is not None and perfect.ease == pytest.approx(EASE_START + 0.1)
    passed = schedule(daily(65))  # q = 3: −0,14
    assert passed is not None and passed.ease == pytest.approx(EASE_START - 0.14)
    failing = schedule(daily(*([0] * 10)))
    assert failing is not None and failing.ease == EASE_MIN


def test_low_ease_shortens_intervals() -> None:
    """Тема, которая давалась тяжело, возвращается чаще той, что давалась легко."""
    hard = schedule(daily(30, 30, 65, 65, 65))
    easy = schedule(daily(95, 95, 95))
    assert hard is not None and easy is not None
    assert hard.interval_days < easy.interval_days


def test_due() -> None:
    sched = schedule(daily(80))
    assert sched is not None
    assert not sched.due(T0 + timedelta(hours=23))
    assert sched.due(T0 + timedelta(days=1))


@pytest.mark.parametrize(
    ("left", "text"),
    [
        (timedelta(0), "сейчас"),
        (timedelta(hours=-5), "сейчас"),
        (timedelta(minutes=10), "через 1 ч"),
        (timedelta(hours=14), "через 14 ч"),
        (timedelta(days=1), "через сутки"),
        (timedelta(hours=23, minutes=50), "через сутки"),
        (timedelta(days=6, hours=2), "через 6 дн."),
    ],
)
def test_due_in(left: timedelta, text: str) -> None:
    assert due_in(T0 + left, T0) == text
