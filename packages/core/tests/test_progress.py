import pytest

from sobesnik_core.progress import (
    Thresholds,
    is_low,
    repeat_topics,
    sort_key,
    topic_stats,
    topic_status,
)

T = Thresholds()


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


def test_repeat_topics() -> None:
    progress = {
        "db.indexes": topic_stats([50, 50]),
        "db.sql": topic_stats([20, 30]),
        "python.asyncio": topic_stats([40]),
        "web.http": topic_stats([30]),
        "go.types": topic_stats([90]),
        "devops.git": topic_stats([80, 80]),
    }
    assert repeat_topics(progress, T, 10) == ["db.sql", "db.indexes", "web.http", "python.asyncio"]
    assert repeat_topics(progress, T, 2) == ["db.sql", "db.indexes"]
    assert repeat_topics({"go.types": topic_stats([90])}, T, 5) == []
