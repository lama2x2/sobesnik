"""Единый JSONL открытых датасетов (EVAL-8, 005 §6): схема и выборка.

Первая строка — заголовок `{"dataset": {...}}`, дальше по строке на ответ.
"""

import json
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sobesnik_core.limits import ANSWER_MAX_CHARS


class DatasetMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    source_url: str | None = None
    license: str = Field(min_length=1)
    lang: str = Field(min_length=2, max_length=10)
    score_key: str | None = None
    """Основная оценка в `human_scores`; None — у датасета нет шкалы."""
    score_min: float | None = None
    score_max: float | None = None
    synthetic: bool = False
    """Оценки — известный уровень ответа, а не человеческие (005 §5.2)."""
    note: str | None = None

    @model_validator(mode="after")
    def _scale(self) -> "DatasetMeta":
        if self.score_key is not None:
            if self.score_min is None or self.score_max is None:
                raise ValueError("у шкалы нужны score_min и score_max")
            if self.score_min >= self.score_max:
                raise ValueError("score_min должен быть меньше score_max")
        return self


class DatasetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=200)
    question_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)
    reference_answer: str | None = Field(default=None, max_length=ANSWER_MAX_CHARS)
    key_points: list[str] = []
    answer: str | None = Field(default=None, max_length=ANSWER_MAX_CHARS)
    audio: str | None = None
    """Путь к аудио относительно файла JSONL — голосовой ответ."""
    human_scores: dict[str, float] = {}

    @model_validator(mode="after")
    def _answer_or_audio(self) -> "DatasetItem":
        if (self.answer is None) == (self.audio is None):
            raise ValueError("нужно ровно одно из answer и audio")
        if self.answer is not None and not self.answer.strip():
            raise ValueError("answer пустой")
        return self


@dataclass(frozen=True)
class ParsedDataset:
    meta: DatasetMeta
    items: list[DatasetItem]


class DatasetFormatError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"строка {line}: {message}")
        self.line = line


def parse_jsonl(lines: Iterable[str]) -> ParsedDataset:
    """Разбирает файл целиком; первая же ошибка — DatasetFormatError с номером строки."""
    meta: DatasetMeta | None = None
    items: list[DatasetItem] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if meta is None:
                if not isinstance(data, dict) or set(data) != {"dataset"}:
                    raise ValueError('первая строка — заголовок {"dataset": {...}}')
                meta = DatasetMeta.model_validate(data["dataset"])
                continue
            item = DatasetItem.model_validate(data)
        except (ValueError, ValidationError) as exc:
            raise DatasetFormatError(number, str(exc)) from exc
        if meta.score_key is not None and meta.score_key not in item.human_scores:
            raise DatasetFormatError(number, f"нет оценки {meta.score_key} в human_scores")
        if item.external_id in seen:
            raise DatasetFormatError(number, f"повтор external_id {item.external_id}")
        seen.add(item.external_id)
        items.append(item)
    if meta is None:
        raise DatasetFormatError(1, "файл пуст")
    if not items:
        raise DatasetFormatError(1, "нет ни одного ответа")
    return ParsedDataset(meta, items)


def stratified_sample(
    items: Sequence[DatasetItem], size: int, score_key: str | None, *, seed: int = 1
) -> list[DatasetItem]:
    """Выборка поровну из корзин по целому баллу `score_key` (остаток — из самых крупных
    корзин). Без шкалы — простая случайная выборка. Порядок исходного файла сохраняется."""
    if size >= len(items):
        return list(items)
    rng = random.Random(seed)
    if score_key is None:
        chosen = set(rng.sample(range(len(items)), size))
        return [item for i, item in enumerate(items) if i in chosen]
    buckets: dict[int, list[int]] = defaultdict(list)
    for i, item in enumerate(items):
        buckets[int(item.human_scores[score_key])].append(i)
    for indices in buckets.values():
        rng.shuffle(indices)
    quota = dict.fromkeys(buckets, 0)
    left = size
    while left:  # по одному в каждую корзину по кругу, пока есть место
        open_buckets = [b for b in sorted(buckets) if quota[b] < len(buckets[b])]
        if len(open_buckets) > left:  # остаток — самым крупным
            open_buckets.sort(key=lambda b: (-(len(buckets[b]) - quota[b]), b))
            open_buckets = open_buckets[:left]
        for b in open_buckets:
            quota[b] += 1
        left -= len(open_buckets)
    chosen = {i for b, indices in buckets.items() for i in indices[: quota[b]]}
    return [item for i, item in enumerate(items) if i in chosen]


def dump_jsonl(meta: dict[str, Any], items: Iterable[dict[str, Any]]) -> str:
    """Текст файла в едином формате: заголовок и строки ответов (для конвертеров research/)."""
    lines = [json.dumps({"dataset": meta}, ensure_ascii=False)]
    lines += [json.dumps(item, ensure_ascii=False) for item in items]
    return "\n".join(lines) + "\n"
