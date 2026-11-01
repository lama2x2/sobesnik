"""Единый JSONL датасетов и выборка (005 §6)."""

import json
from collections import Counter
from typing import Any

import pytest

from sobesnik_core.dataset import DatasetFormatError, parse_jsonl, stratified_sample

META = {
    "name": "mohler",
    "license": "CC-BY-4.0",
    "lang": "en",
    "score_key": "avg",
    "score_min": 0,
    "score_max": 5,
}


def item(n: int, score: float = 3.0, **extra: Any) -> dict[str, Any]:
    row = {
        "external_id": f"a{n}",
        "question_id": f"q{n % 3}",
        "question": "What is a stack?",
        "reference_answer": "LIFO structure.",
        "answer": f"answer {n}",
        "human_scores": {"avg": score},
    }
    row.update(extra)
    return {k: v for k, v in row.items() if v is not None}


def lines(*rows: dict[str, Any], meta: dict[str, Any] | None = None) -> list[str]:
    return [json.dumps({"dataset": meta or META}), *(json.dumps(r) for r in rows)]


def test_parse_valid() -> None:
    parsed = parse_jsonl([*lines(item(1), item(2, audio="x.ogg", answer=None)), ""])
    assert parsed.meta.name == "mohler"
    assert [i.external_id for i in parsed.items] == ["a1", "a2"]
    assert parsed.items[1].audio == "x.ogg"


@pytest.mark.parametrize(
    ("rows", "line", "message"),
    [
        ([item(1, audio="x.ogg")], 2, "ровно одно"),
        ([item(1, answer=None)], 2, "ровно одно"),
        ([item(1, answer="   ")], 2, "пустой"),
        ([item(1), {**item(2), "human_scores": {}}], 3, "нет оценки avg"),
        ([item(1), item(1)], 3, "повтор"),
        ([{**item(1), "extra": 1}], 2, "extra"),
        ([], 1, "нет ни одного"),
    ],
)
def test_parse_errors(rows: list[dict[str, Any]], line: int, message: str) -> None:
    with pytest.raises(DatasetFormatError) as exc:
        parse_jsonl(lines(*rows))
    assert exc.value.line == line
    assert message in str(exc.value)


def test_parse_bad_header() -> None:
    with pytest.raises(DatasetFormatError, match="заголовок"):
        parse_jsonl([json.dumps(item(1))])
    with pytest.raises(DatasetFormatError, match="score_min"):
        parse_jsonl(lines(item(1), meta={**META, "score_min": None}))
    with pytest.raises(DatasetFormatError, match="пуст"):
        parse_jsonl([])


def test_no_scale_needs_no_scores() -> None:
    meta = {"name": "voice", "license": "private", "lang": "ru"}
    parsed = parse_jsonl(lines(item(1, human_scores={}), meta=meta))
    assert parsed.meta.score_key is None


def test_stratified_sample() -> None:
    parsed = parse_jsonl(lines(*(item(n, score=n % 6 + (0.5 if n % 2 else 0)) for n in range(60))))
    sample = stratified_sample(parsed.items, 18, "avg", seed=1)
    assert len(sample) == 18
    assert Counter(int(i.human_scores["avg"]) for i in sample) == dict.fromkeys(range(6), 3)
    assert sample == stratified_sample(parsed.items, 18, "avg", seed=1)  # детерминирована
    assert sample != stratified_sample(parsed.items, 18, "avg", seed=2)
    order = [i.external_id for i in parsed.items]
    assert [i.external_id for i in sample] == sorted(
        (i.external_id for i in sample), key=order.index
    )


def test_stratified_sample_small_buckets() -> None:
    # корзина 0 маленькая: недостающее добирается из крупных
    rows = [item(0, score=0), *(item(n, score=4) for n in range(1, 20))]
    rows += [item(n, score=2) for n in range(20, 30)]
    sample = stratified_sample(parse_jsonl(lines(*rows)).items, 9, "avg")
    counts = Counter(int(i.human_scores["avg"]) for i in sample)
    assert counts[0] == 1
    assert counts[2] + counts[4] == 8
    assert counts[4] >= counts[2]


def test_sample_bigger_than_dataset_and_without_scale() -> None:
    parsed = parse_jsonl(lines(item(1), item(2)))
    assert stratified_sample(parsed.items, 5, "avg") == parsed.items
    plain = stratified_sample(parsed.items, 1, None, seed=3)
    assert len(plain) == 1
