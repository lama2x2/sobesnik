"""Рубрика оценки ответа (000 §9, 003 §5): критерии, веса, итог и правило «вопрос пройден».

Рубрика — файл rubrics/v<N>.yaml. Версия — номер и хеш файла, как у промптов.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import yaml

from sobesnik_core.versioned import read_version, resource_dir, version_tag

LEVELS = (0, 1, 2, 3)
MAX_SCORE = LEVELS[-1]
_ID_RE = re.compile(r"^[a-z_]+$")


@dataclass(frozen=True)
class Criterion:
    id: str
    title: str
    weight: float
    levels: Mapping[int, str]
    """Описание уровней 0–3."""


@dataclass(frozen=True)
class Rubric:
    version: str
    """Например `v1#3f2a9c1e`."""
    criteria: tuple[Criterion, ...]
    min_total: int
    min_scores: Mapping[str, int]
    notes: str

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.criteria]

    def _checked(self, scores: Mapping[str, int]) -> Mapping[str, int]:
        if set(scores) != set(self.ids):
            raise ValueError(f"баллы {sorted(scores)} не совпадают с критериями {self.ids}")
        for criterion, score in scores.items():
            if score not in LEVELS:
                raise ValueError(f"{criterion}: балл {score} вне 0–{MAX_SCORE}")
        return scores

    def total(self, scores: Mapping[str, int]) -> int:
        """Σ(вес × балл / 3) × 100 с округлением половины вверх."""
        scores = self._checked(scores)
        raw = sum(Decimal(str(c.weight)) * scores[c.id] / MAX_SCORE * 100 for c in self.criteria)
        return int(Decimal(raw).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def passed(self, scores: Mapping[str, int]) -> bool:
        if self.total(scores) < self.min_total:
            return False
        return all(scores[c] >= minimum for c, minimum in self.min_scores.items())

    def render(self) -> str:
        """Текст рубрики для промпта оценки."""
        blocks = []
        for c in self.criteria:
            levels = "\n".join(f"  {score} — {text}" for score, text in c.levels.items())
            blocks.append(f"{c.id} ({c.title}, вес {c.weight:g}):\n{levels}")
        return "\n\n".join(blocks) + "\n\nПравила:\n" + self.notes.strip()


def parse_rubric(number: int, raw: bytes) -> Rubric:
    data: dict[str, Any] = yaml.safe_load(raw.decode("utf-8"))
    criteria = []
    for item in data["criteria"]:
        levels = {int(k): str(v) for k, v in item["levels"].items()}
        criteria.append(
            Criterion(
                id=item["id"],
                title=item["title"],
                weight=float(item["weight"]),
                levels=dict(sorted(levels.items())),
            )
        )
    rule = data.get("pass_rule") or {}
    rubric = Rubric(
        version=version_tag(number, raw),
        criteria=tuple(criteria),
        min_total=int(rule.get("min_total", 0)),
        min_scores={str(k): int(v) for k, v in (rule.get("min_scores") or {}).items()},
        notes=str(data.get("notes") or ""),
    )
    _validate(rubric)
    return rubric


def _validate(rubric: Rubric) -> None:
    ids = rubric.ids
    if not ids:
        raise ValueError("в рубрике нет критериев")
    if len(set(ids)) != len(ids):
        raise ValueError("id критериев повторяются")
    for c in rubric.criteria:
        if not _ID_RE.match(c.id):
            raise ValueError(f"неверный id критерия: {c.id!r}")
        if c.weight <= 0:
            raise ValueError(f"{c.id}: вес должен быть положительным")
        if tuple(c.levels) != LEVELS:
            raise ValueError(f"{c.id}: уровни должны быть ровно {LEVELS}")
    if abs(sum(c.weight for c in rubric.criteria) - 1) > 1e-6:
        raise ValueError("сумма весов критериев должна быть 1")
    unknown = set(rubric.min_scores) - set(ids)
    if unknown:
        raise ValueError(f"в pass_rule неизвестные критерии: {sorted(unknown)}")


def load_rubric(number: int | None = None) -> Rubric:
    """Рубрика версии `number`; без номера — последняя версия."""
    number, raw = read_version(resource_dir("rubrics"), "рубрика", number)
    return parse_rubric(number, raw)
