"""Метрики качества оценки (005 §11): согласие с людьми, между моделями и влияние распознавания.

Без I/O и внешних зависимостей: выборки — десятки ответов, numpy и scipy для этого не нужны.
"""

import math
import random
import re
import statistics
from collections.abc import Callable, Sequence

Pair = tuple[float, float]


def to_percent(value: float, low: float, high: float) -> float:
    """Оценка по шкале датасета → 0–100, как итог модели (005 §11.1)."""
    return (value - low) / (high - low) * 100


def round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def to_scale(total: float, low: float, high: float) -> int:
    """Итог модели 0–100 → целая категория шкалы датасета (для QWK)."""
    return round_half_up(total / 100 * (high - low) + low)


def _ranks(values: Sequence[float]) -> list[float]:
    """Ранги с 1; у равных значений — средний ранг."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return math.nan
    return sxy / math.sqrt(sxx * syy)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Ранговая корреляция Спирмена; NaN, если пар меньше двух или одна сторона постоянна."""
    if len(xs) != len(ys):
        raise ValueError("разная длина выборок")
    if len(xs) < 2:
        return math.nan
    return _pearson(_ranks(xs), _ranks(ys))


def qwk(xs: Sequence[int], ys: Sequence[int], low: int, high: int) -> float:
    """Квадратично взвешенная каппа Коэна для категорий low…high."""
    if len(xs) != len(ys):
        raise ValueError("разная длина выборок")
    if not xs:
        return math.nan
    size = high - low + 1
    observed = [[0.0] * size for _ in range(size)]
    for x, y in zip(xs, ys, strict=True):
        if not (low <= x <= high and low <= y <= high):
            raise ValueError(f"категория вне {low}…{high}: {x}, {y}")
        observed[x - low][y - low] += 1
    n = len(xs)
    rows = [sum(r) for r in observed]
    cols = [sum(observed[i][j] for i in range(size)) for j in range(size)]
    num = den = 0.0
    for i in range(size):
        for j in range(size):
            weight = (i - j) ** 2 / (size - 1) ** 2 if size > 1 else 0.0
            num += weight * observed[i][j]
            den += weight * rows[i] * cols[j] / n
    if den == 0:
        return 1.0 if num == 0 else math.nan
    return 1 - num / den


def mae(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys):
        raise ValueError("разная длина выборок")
    if not xs:
        return math.nan
    return statistics.fmean(abs(x - y) for x, y in zip(xs, ys, strict=True))


def bootstrap_ci(
    pairs: Sequence[Pair],
    metric: Callable[[list[Pair]], float],
    *,
    repeats: int = 1000,
    seed: int = 1,
    level: float = 0.95,
) -> tuple[float, float]:
    """Доверительный интервал метрики бутстрепом по парам; NaN-повторы выбрасываются."""
    if len(pairs) < 2:
        return math.nan, math.nan
    rng = random.Random(seed)
    values = sorted(
        v
        for v in (metric([rng.choice(pairs) for _ in pairs]) for _ in range(repeats))
        if not math.isnan(v)
    )
    if not values:
        return math.nan, math.nan
    tail = (1 - level) / 2
    lo = values[int(tail * (len(values) - 1))]
    hi = values[int((1 - tail) * (len(values) - 1))]
    return lo, hi


_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_words(text: str) -> list[str]:
    """Слова для WER: регистр, пунктуация и «ё» не считаются ошибками."""
    return _PUNCT.sub(" ", text.lower().replace("ё", "е")).split()


def wer(reference: str, hypothesis: str) -> float:
    """Доля ошибок по словам: расстояние Левенштейна по словам / длина эталона."""
    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, start=1):
        prev, row[0] = row[0], i
        for j, other in enumerate(hyp, start=1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (word != other))
    return row[-1] / max(len(ref), 1)
