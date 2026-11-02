"""Скрипты research/ спринта 5: конвертеры датасетов и отчёт (005 §5, §11). Без сети и LLM."""

import csv
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from sobesnik_api.export import COMMON, TAIL
from sobesnik_core.dataset import dump_jsonl, parse_jsonl

RESEARCH = Path(__file__).resolve().parents[3] / "research"


def load(relative: str) -> ModuleType:
    path = RESEARCH / relative
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mohler_convert(tmp_path: Path) -> None:
    mohler = load("datasets/mohler.py")
    rows = [
        {
            "id": "E01.Q01.A00",
            "question": "What is a <b>stack</b>?",
            "instructor_answer": "LIFO &amp; push/pop.",
            "student_answer": "Last in,<br><br> first out.",
            "score_grader_1": 4.0,
            "score_grader_2": 5.0,
            "score_avg": 4.5,
        },
        {
            **{"id": "E01.Q01.A01", "question": "Q", "instructor_answer": "R"},
            "student_answer": "<br>",
            "score_grader_1": 0,
            "score_grader_2": 0,
            "score_avg": 0,
        },
    ]
    items = mohler.convert(rows)
    assert len(items) == 1  # пустой ответ студента выброшен
    assert items[0]["question_id"] == "E01.Q01"
    assert items[0]["question"] == "What is a stack?"
    assert items[0]["reference_answer"] == "LIFO & push/pop."
    assert items[0]["answer"] == "Last in, first out."
    parsed = parse_jsonl(dump_jsonl(mohler.META, items).splitlines())
    exam = {"score_grader_1": 7.5, "score_grader_2": 3.0, "score_avg": 3.0}
    assert mohler.human_scores(exam) == {"avg": 3.0}  # 0–10 вперемешку с 0–5
    assert parsed.items[0].human_scores == {"grader_1": 4.0, "grader_2": 5.0, "avg": 4.5}


BANK = """# Python

## Генераторы

### Что такое генератор

Генератор — функция, которая возвращает итератор и отдаёт значения через yield. Он хранит
состояние между вызовами. Генераторы экономят память, потому что не строят весь список сразу.
Их удобно использовать для потоковой обработки данных и бесконечных последовательностей.

### Вопрос с кодом

```python
# Это не заголовок
def f(): yield 1
```

## Исключения

### Что такое finally

Блок finally выполняется всегда: и при нормальном выходе, и при исключении. В нём закрывают
файлы и соединения. Даже return внутри try не помешает finally выполниться перед выходом.
Это гарантия освобождения ресурсов в любом случае, даже при ошибке в обработчике except.

# БД

### Что такое индекс

Индекс — отдельная структура, которая ускоряет поиск строк по значению столбца, но замедляет
вставку и занимает место. Чаще всего это B-tree, бывают hash, GIN и GiST. Его стоит строить по
столбцам, по которым часто фильтруют и сортируют.
"""


def test_ru_bank_ladder() -> None:
    ru_bank = load("datasets/ru_bank.py")
    entries = ru_bank.parse_bank(BANK)
    assert [e.question for e in entries] == [
        "Что такое генератор",
        "Вопрос с кодом",
        "Что такое finally",
        "Что такое индекс",
    ]
    assert entries[1].has_code
    assert [e.section for e in entries] == ["Python", "Python", "Python", "БД"]
    suitable = [e.question for e in entries if ru_bank.suitable(e)]
    assert suitable == ["Что такое генератор", "Что такое finally"]  # без кода и без БД

    items = ru_bank.ladder(entries, questions=2, seed=1)
    assert len(items) == 8
    by_id = {i["external_id"]: i for i in items}
    q = by_id["q1.full"]["question"]
    assert by_id["q1.full"]["answer"] == by_id["q1.full"]["reference_answer"]
    assert by_id["q1.half"]["answer"] in by_id["q1.full"]["answer"]
    assert len(by_id["q1.half"]["answer"]) < len(by_id["q1.full"]["answer"])
    assert by_id["q1.other"]["answer"] != by_id["q1.full"]["answer"]
    assert by_id["q1.dont_know"]["answer"] == ru_bank.DONT_KNOW
    assert [
        by_id[f"q1.{k}"]["human_scores"]["level"] for k in ("full", "half", "other", "dont_know")
    ] == [3, 2, 0, 0]
    assert all(by_id[f"q1.{k}"]["question"] == q for k in ("half", "other", "dont_know"))


def test_voice_fragments(tmp_path: Path) -> None:
    voice = load("datasets/voice_fragments.py")
    (tmp_path / "a.ogg").write_bytes(b"x")
    items = voice.convert([{"name": "a", "question": "Что такое GIL?"}], tmp_path)
    assert items == [
        {"external_id": "a", "question_id": "a", "question": "Что такое GIL?", "audio": "a.ogg"}
    ]


def row(**values: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict.fromkeys(COMMON + TAIL, "")
    base.update(status="done", latency_ms=10000, prompt_version="v3#abc", temperature=0.0, seed=42)
    base.update(values)
    return base


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COMMON + TAIL)
        writer.writeheader()
        writer.writerows(rows)


def test_report(tmp_path: Path) -> None:
    report = load("quality/report.py")
    rows = []
    n = 0
    for run, model in (("r1", "a"), ("r2", "b"), ("r1-repeat", "a")):
        for i, (total, human) in enumerate([(10, 0.5), (40, 2.0), (70, 3.5), (95, 5.0)]):
            n += 1
            rows.append(
                row(
                    evaluation_id=f"e{n}",
                    answer_id=f"m{i}",
                    run_id=run,
                    run_name=run,
                    model=model,
                    total=total if model == "a" else 100 - total,
                    passed=total >= 50,
                    dataset="mohler-4",
                    dataset_item=f"i{i}",
                    human_score=human,
                    human_score_max=5,
                    human_scores=json.dumps({"grader_1": human, "grader_2": human, "avg": human}),
                )
            )
    for kind, model_total in (("full", 90), ("half", 60), ("other", 5), ("dont_know", 0)):
        n += 1
        rows.append(
            row(
                evaluation_id=f"e{n}",
                answer_id=f"l{kind}",
                run_id="lad",
                run_name="lad",
                model="a",
                question_id="q1",
                total=model_total,
                dataset="ru-ladder",
                human_score={"full": 3, "half": 2}.get(kind, 0),
                human_score_max=3,
                human_scores=json.dumps({"level": {"full": 3, "half": 2}.get(kind, 0)}),
            )
        )
    for kind, text, total in (
        ("asr", "ивент лук крутится", 60),
        ("reference", "event loop крутится", 70),
    ):
        n += 1
        rows.append(
            row(
                evaluation_id=f"e{n}",
                answer_id="v1",
                run_id=kind,
                run_name=f"voice-{kind}",
                model="a",
                transcript_kind=kind,
                answer=text,
                total=total,
                dataset="voice-1",
                dataset_item="v01",
                asr_doubts="распознано «ивент лук» — возможно, сказано «event loop»"
                if kind == "asr"
                else "",
            )
        )
    rows.append(
        row(
            evaluation_id="f1",
            answer_id="m9",
            run_id="r2",
            run_name="r2",
            model="b",
            status="failed",
            dataset="mohler-4",
        )
    )
    rows.append(
        row(evaluation_id="live1", answer_id="u1", total=50, key_points="a | b", wrong_points="b")
    )
    path = tmp_path / "eval.csv"
    write_csv(path, rows)

    out = tmp_path / "report.md"
    report.main([str(path), "-o", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "| r1 | a | v3 |" in text
    assert "| 4 | 0% | 10.0 | 1.00 [" in text  # r1: идеальное согласие с людьми
    assert "| r2 | b | v3 | 0.0 | mohler-4 | 4 | 20% |" in text  # один сбой из пяти
    assert "| ru-ladder | 4 | 0% | 10.0 | — | — | — | 1.00 | 0.95 |" in text  # лестница
    assert "человек × человек на тех же 4 ответах: ρ = 1.00" in text
    assert "| r1 | r2 | 4 | -1.00 |" in text
    assert "итог совпал у 100% из 4 ответов" in text
    assert "средняя |Δ| = 10.0" in text
    assert "| v01 | 67% | -10 |" in text
    assert "1 сомнений на 1 ответов" in text
    assert "Непустые `wrong_points` у 1 из 1" in text
    assert (tmp_path / "delta_wer.png").stat().st_size > 1000
