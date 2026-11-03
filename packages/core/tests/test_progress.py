from datetime import UTC, datetime, timedelta

import pytest

from sobesnik_core.progress import (
    Thresholds,
    TopicStats,
    is_low,
    next_due,
    repeat_topics,
    sort_key,
    stats_from_history,
    topic_stats,
    topic_status,
)

T = Thresholds()
NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def history(*scores: int, days_ago: int = 0) -> TopicStats:
    """Попытки раз в день, последняя — `days_ago` дней назад; баллы — старые первыми."""
    last = NOW - timedelta(days=days_ago)
    attempts = [(s, last - timedelta(days=len(scores) - 1 - i)) for i, s in enumerate(scores)]
    return stats_from_history(list(reversed(attempts)))


def test_stats_keep_three_newest() -> None:
    stats = topic_stats([40, 85, 70, 10])
    assert stats.attempts == 4
    assert stats.last_scores == (40, 85, 70)
    assert stats.last_score == 40
    assert stats.avg_last3 == pytest.approx(65)


def test_stats_with_explicit_attempts() -> None:
    assert topic_stats([50, 60, 70], attempts=9).attempts == 9


def test_empty() -> None:
    stats = topic_stats([])
    assert stats.last_score is None
    assert stats.avg_last3 is None
    assert topic_status(stats, T) == "few"


@pytest.mark.parametrize(
    ("scores", "status"),
    [
        ([40], "few"),  # одна оценка тему слабой не делает (007 §14 п. 1)
        ([95], "few"),
        ([85, 40], "ok"),  # 62,5 — пример из 007 §5
        ([85, 40, 35], "weak"),  # 53
        ([40, 80, 85], "ok"),  # последний балл < 50 сам по себе не важен (п. 2)
        ([60, 59], "weak"),  # 59,5
        ([60, 60], "ok"),
    ],
)
def test_status(scores: list[int], status: str) -> None:
    assert topic_status(topic_stats(scores), T) == status


def test_thresholds_from_config() -> None:
    strict = Thresholds(min_attempts=3, weak_avg=70)
    assert topic_status(topic_stats([65, 65]), strict) == "few"
    assert topic_status(topic_stats([65, 65, 65]), strict) == "weak"


def test_is_low() -> None:
    assert is_low(topic_stats([40]), T)
    assert not is_low(topic_stats([75]), T)
    assert not is_low(topic_stats([40, 40]), T)  # уже слабая, а не «мало данных»


def test_sort_key_order() -> None:
    items = {
        "ok": topic_stats([80, 80]),
        "few": topic_stats([30]),
        "weak_hi": topic_stats([55, 55]),
        "weak_lo": topic_stats([20, 30]),
    }
    ordered = sorted(items, key=lambda s: sort_key(items[s], T))
    assert ordered == ["weak_lo", "weak_hi", "few", "ok"]


def test_stats_from_history() -> None:
    stats = history(40, 85, 70, 10)
    assert stats.attempts == 4
    assert stats.last_scores == (10, 70, 85)
    assert stats.schedule is not None and stats.schedule.due_at == NOW  # провал — сейчас


def test_repeat_topics_by_due_date() -> None:
    """Провал — сразу в повторение; сдал — через день, потом через 6 (011 §5.3)."""
    progress = {
        "db.indexes": history(50, 50),
        "db.sql": history(20, 30),
        "python.asyncio": history(40),
        "web.http": history(30),
        "go.types": history(90, days_ago=1),  # сдал вчера → срок сейчас
        "devops.git": history(80, 80),  # сдал дважды → через 6 дней
        "devops.docker": history(70, days_ago=0),  # сдал сегодня → завтра
    }
    assert repeat_topics(progress, T, NOW, 10) == [
        "db.sql",
        "db.indexes",
        "web.http",
        "python.asyncio",
        "go.types",
    ]
    assert repeat_topics(progress, T, NOW, 2) == ["db.sql", "db.indexes"]
    assert repeat_topics({"go.types": history(90, days_ago=0)}, T, NOW, 5) == []


def test_repeat_topics_without_schedule_are_due() -> None:
    """Строка до пересчёта после миграции: срока нет — значит, «сейчас»."""
    assert repeat_topics({"go.types": topic_stats([90])}, T, NOW, 5) == ["go.types"]


def test_force_takes_nearest_due() -> None:
    progress = {
        "devops.git": history(80, 80, days_ago=0),  # через 6 дней
        "devops.docker": history(70, days_ago=0),  # завтра
        "go.types": history(95, days_ago=0),  # завтра, но балл выше
    }
    assert repeat_topics(progress, T, NOW, 5) == []
    assert repeat_topics(progress, T, NOW, 2, force=True) == ["devops.docker", "go.types"]
    assert next_due(progress) == ("devops.docker", NOW + timedelta(days=1))
    assert next_due({}) is None
