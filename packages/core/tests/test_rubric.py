import hashlib

import pytest

from sobesnik_core.rubric import load_rubric, parse_rubric

RAW = b"""\
pass_rule:
  min_total: 50
  min_scores: {a: 2}
criteria:
  - id: a
    title: A
    weight: 0.6
    levels: {0: no, 1: bad, 2: ok, 3: good}
  - id: b
    title: B
    weight: 0.4
    levels: {0: no, 1: bad, 2: ok, 3: good}
notes: |
  rule
"""


def test_package_rubric_v1() -> None:
    rubric = load_rubric()
    assert rubric.version.startswith("v1#")
    assert rubric.ids == ["correctness", "completeness", "depth", "terminology", "structure"]
    assert [c.weight for c in rubric.criteria] == [0.30, 0.25, 0.20, 0.15, 0.10]
    assert all(tuple(c.levels) == (0, 1, 2, 3) for c in rubric.criteria)
    assert (rubric.min_total, dict(rubric.min_scores)) == (50, {"correctness": 2})
    assert load_rubric(1) == rubric


def test_version_and_hash() -> None:
    rubric = parse_rubric(2, RAW)
    assert rubric.version == "v2#" + hashlib.sha256(RAW).hexdigest()[:8]
    assert parse_rubric(2, RAW.replace(b"rule", b"rules")).version != rubric.version


def test_unknown_version() -> None:
    with pytest.raises(FileNotFoundError):
        load_rubric(99)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (b"weight: 0.4", b"weight: 0.5", "сумма весов"),
        (b"{a: 2}", b"{c: 2}", "неизвестные критерии"),
        (b"id: b", b"id: a", "повторяются"),
        (b"id: b", b"id: B-1", "неверный id"),
        (b"{0: no, 1: bad, 2: ok, 3: good}\nnotes", b"{0: no, 1: bad, 2: ok}\nnotes", "уровни"),
    ],
)
def test_broken_rubric(old: bytes, new: bytes, message: str) -> None:
    assert old in RAW
    with pytest.raises(ValueError, match=message):
        parse_rubric(1, RAW.replace(old, new))


def test_total() -> None:
    rubric = load_rubric(1)
    ids = rubric.ids
    assert rubric.total(dict.fromkeys(ids, 3)) == 100
    assert rubric.total(dict.fromkeys(ids, 0)) == 0
    # (0.30·3 + 0.25·2 + 0.20·1 + 0.15·2 + 0.10·0) / 3 · 100 = 1.9 / 3 · 100 = 63.33
    scores = dict(zip(ids, [3, 2, 1, 2, 0], strict=True))
    assert rubric.total(scores) == 63


def test_total_rejects_wrong_scores() -> None:
    rubric = load_rubric(1)
    with pytest.raises(ValueError, match="не совпадают"):
        rubric.total({"correctness": 3})
    with pytest.raises(ValueError, match="вне"):
        rubric.total(dict.fromkeys(rubric.ids, 4))


def test_passed() -> None:
    rubric = load_rubric(1)
    # все по 2: итог 67, корректность 2 — пройден
    assert rubric.passed(dict.fromkeys(rubric.ids, 2)) is True
    # корректность 2, но итог 43 — нет
    low = {"correctness": 2, "completeness": 1, "depth": 1, "terminology": 1, "structure": 1}
    assert rubric.total(low) == 43
    assert rubric.passed(low) is False
    # итог высокий, но корректность 1 — нет
    shaky = {"correctness": 1, "completeness": 3, "depth": 3, "terminology": 3, "structure": 3}
    assert rubric.total(shaky) >= 50
    assert rubric.passed(shaky) is False


def test_render_contains_levels_and_notes() -> None:
    text = load_rubric(1).render()
    assert "correctness (Корректность, вес 0.3):" in text
    assert "  3 — ошибок нет" in text
    assert "Ответ устный" in text
