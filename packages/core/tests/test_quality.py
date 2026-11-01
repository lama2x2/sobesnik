"""Метрики качества оценки (005 §11)."""

import math

import pytest

from sobesnik_core.quality import (
    bootstrap_ci,
    mae,
    normalize_words,
    qwk,
    spearman,
    to_percent,
    to_scale,
    wer,
)


def test_scale_conversion() -> None:
    assert to_percent(3.5, 0, 5) == 70
    assert to_percent(0, 0, 5) == 0
    assert to_scale(73, 0, 5) == 4
    assert to_scale(50, 0, 5) == 3  # половина — вверх
    assert to_scale(0, 0, 5) == 0
    assert to_scale(100, 0, 5) == 5


def test_spearman() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1)
    # связи в рангах получают средний ранг
    assert spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(math.sqrt(3) / 2)
    assert math.isnan(spearman([1], [1]))
    assert math.isnan(spearman([1, 1, 1], [1, 2, 3]))  # постоянная сторона
    with pytest.raises(ValueError):
        spearman([1, 2], [1])


def test_qwk() -> None:
    assert qwk([0, 1, 2, 3], [0, 1, 2, 3], 0, 3) == pytest.approx(1)
    assert qwk([0, 1, 2], [2, 1, 0], 0, 2) == pytest.approx(-1)
    # промах на одну категорию лучше, чем на три
    near = qwk([0, 1, 2, 3, 4, 5], [1, 1, 2, 3, 4, 5], 0, 5)
    far = qwk([0, 1, 2, 3, 4, 5], [3, 1, 2, 3, 4, 5], 0, 5)
    assert 0 < far < near < 1
    assert qwk([2, 2], [2, 2], 0, 5) == 1.0  # всё совпало, ожидание вырождено
    assert math.isnan(qwk([], [], 0, 5))
    with pytest.raises(ValueError):
        qwk([6], [0], 0, 5)


def test_mae() -> None:
    assert mae([0, 50, 100], [10, 50, 70]) == pytest.approx(40 / 3)
    assert math.isnan(mae([], []))


def test_bootstrap_ci_is_deterministic() -> None:
    pairs = [(float(i), float(i)) for i in range(10)]
    assert bootstrap_ci(pairs, lambda p: spearman(*zip(*p, strict=True))) == (1.0, 1.0)
    noisy = [(float(i), float((i * 7) % 10)) for i in range(10)]

    def rho(p: list[tuple[float, float]]) -> float:
        return spearman([a for a, _ in p], [b for _, b in p])

    first = bootstrap_ci(noisy, rho, repeats=200)
    assert first == bootstrap_ci(noisy, rho, repeats=200)
    assert first[0] < first[1]
    assert all(math.isnan(v) for v in bootstrap_ci(pairs[:1], rho))


def test_wer() -> None:
    assert wer("Привет, мир!", "привет мир") == 0
    assert wer("ещё раз", "еще раз") == 0
    assert wer("a b c", "a x c") == pytest.approx(1 / 3)
    assert wer("a b c", "a c") == pytest.approx(1 / 3)
    assert wer("a b", "a b c d") == pytest.approx(1)
    assert wer("", "слово") == 1  # пустой эталон не делит на ноль
    assert normalize_words("Event-loop, asyncio!") == ["event", "loop", "asyncio"]
