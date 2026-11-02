"""Mohler ASAG 2011 → единый JSONL (005 §5.1).

    docker compose run --rm tools uv run python research/datasets/mohler.py
    docker compose run --rm tools uv run sobesnik-cli dataset import \
        research/data/datasets/mohler.jsonl --sample 30

81 вопрос курса «структуры данных» (University of North Texas), 2 273 ответа студентов,
эталон преподавателя и оценки двух людей 0–5. Лицензия зеркала — CC BY 4.0. Скачивается
только JSON через datasets-server Hugging Face (без pyarrow) в research/data/datasets/raw/mohler/;
повторный запуск берёт уже скачанное.

Цитирование: Mohler, Bunescu, Mihalcea. Learning to Grade Short Answer Questions using Semantic
Similarity Measures and Dependency Graph Alignments. ACL-HLT 2011.
"""

import html
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from sobesnik_core.dataset import dump_jsonl

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "research" / "data" / "datasets" / "raw" / "mohler" / "rows.json"
OUT = ROOT / "research" / "data" / "datasets" / "mohler.jsonl"
DATASET = "nkazi/MohlerASAG"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
PAGE = 100

META = {
    "name": "mohler",
    "source_url": f"https://huggingface.co/datasets/{DATASET}",
    "license": "CC-BY-4.0",
    "lang": "en",
    "score_key": "avg",
    "score_min": 0,
    "score_max": 5,
    "synthetic": False,
    "note": "Mohler, Bunescu, Mihalcea, ACL-HLT 2011; open_ended, raw; "
    "grader_1/2 только у заданий E01–E10",
}
META_MAX = 5

_BREAK = re.compile(r"<br\s*/?>|</?p>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    """Убирает HTML (`<br>` — в пробел, прочие теги — без следа), сущности и лишние пробелы."""
    return " ".join(html.unescape(_TAG.sub("", _BREAK.sub(" ", text))).split())


def download() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=60) as client:
        while True:
            params: dict[str, str | int] = {
                "dataset": DATASET,
                "config": "raw",
                "split": "open_ended",
                "offset": len(rows),
                "length": PAGE,
            }
            page = client.get(ROWS_URL, params=params).raise_for_status().json()
            rows += [r["row"] for r in page["rows"]]
            print(f"скачано {len(rows)}/{page['num_rows_total']}", file=sys.stderr)
            if len(rows) >= page["num_rows_total"] or not page["rows"]:
                return rows


def human_scores(row: dict[str, Any]) -> dict[str, float]:
    """`avg` — всегда 0–5. Оценки отдельных людей на экзаменах (E11, E12) стоят то в 0–10, то в
    0–5 вперемешку, поэтому берутся только у заданий, где обе в 0–5."""
    scores = {"avg": row["score_avg"]}
    if max(row["score_grader_1"], row["score_grader_2"]) <= META_MAX:
        scores |= {"grader_1": row["score_grader_1"], "grader_2": row["score_grader_2"]}
    return scores


def convert(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        answer = clean(row["student_answer"])
        if not answer:  # пустые ответы студентов оценивать нечем
            continue
        external_id = row["id"]
        items.append(
            {
                "external_id": external_id,
                "question_id": external_id.rsplit(".", 1)[0],
                "question": clean(row["question"]),
                "reference_answer": clean(row["instructor_answer"]),
                "answer": answer,
                "human_scores": human_scores(row),
            }
        )
    return items


def main() -> None:
    if RAW.exists():
        rows = json.loads(RAW.read_text(encoding="utf-8"))
    else:
        rows = download()
        RAW.parent.mkdir(parents=True, exist_ok=True)
        RAW.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    items = convert(rows)
    OUT.write_text(dump_jsonl(META, items), encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {len(items)} ответов")


if __name__ == "__main__":
    main()
