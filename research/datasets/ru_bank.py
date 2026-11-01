"""Русский банк вопросов к Python-собеседованию → JSONL «лестницы» (005 §5.2).

    docker compose run --rm tools uv run python research/datasets/ru_bank.py
    docker compose run --rm tools uv run sobesnik-cli dataset import \
        research/data/datasets/ru_ladder.jsonl

Источник — github.com/yakimka/python_interview_questions (MIT) на зафиксированном коммите.
Людских оценок в банке нет, поэтому к каждому вопросу — 4 ответа известного качества:
эталон целиком (3), его первая половина (2), эталон другого вопроса (0) и «не знаю» (0).
Хорошая модель ставит им баллы в этом порядке.
"""

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from sobesnik_core.dataset import dump_jsonl

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "d4e0aed6ee8a20e92375d53f11aeac0ace78d2e9"
URL = f"https://raw.githubusercontent.com/yakimka/python_interview_questions/{COMMIT}/questions.md"
RAW = ROOT / "research" / "data" / "datasets" / "raw" / "ru_bank" / "questions.md"
OUT = ROOT / "research" / "data" / "datasets" / "ru_ladder.jsonl"

SECTIONS = ("Python", "Веб-разработка", "HTTP", "Алгоритмы, структуры")
"""Разделы верхнего уровня. Раздел про БД автор банка сам называет неточным."""
MIN_CHARS, MAX_CHARS = 200, 1500
QUESTIONS = 8
DONT_KNOW = "Не знаю, не сталкивался с этим."

META = {
    "name": "ru-ladder",
    "source_url": f"https://github.com/yakimka/python_interview_questions/tree/{COMMIT}",
    "license": "MIT",
    "lang": "ru",
    "score_key": "level",
    "score_min": 0,
    "score_max": 3,
    "synthetic": True,
    "note": "лестница: эталон 3, половина 2, чужой эталон 0, «не знаю» 0",
}


@dataclass(frozen=True)
class Entry:
    section: str
    """Раздел верхнего уровня."""
    subsection: str
    question: str
    answer: str
    has_code: bool


def parse_bank(text: str) -> list[Entry]:
    """Заголовки `#`, `##`, `###` вне блоков кода; ответ — текст до следующего заголовка."""
    entries: list[Entry] = []
    section = subsection = ""
    question: str | None = None
    body: list[str] = []
    fence = False

    def flush() -> None:
        if question is not None:
            answer = "\n".join(body).strip()
            entries.append(Entry(section, subsection, question, answer, "```" in answer))

    for line in text.splitlines():
        if line.startswith("```"):
            fence = not fence
        heading = None if fence else re.match(r"^(#{1,3}) (.+)$", line)
        if heading is None:
            body.append(line)
            continue
        flush()
        level, title = len(heading.group(1)), heading.group(2).strip()
        question, body = None, []
        if level == 1:
            section, subsection = title, ""
        elif level == 2:
            subsection = title
        else:
            question = title
    flush()
    return entries


def plain(text: str) -> str:
    """Markdown → простой текст: ссылки, выделение и списки без разметки."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`]{1,3}", "", text)
    text = re.sub(r"^\s*[-*+] ", "", text, flags=re.MULTILINE)
    return " ".join(text.split())


def first_half(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) < 2:
        words = text.split()
        return " ".join(words[: max(len(words) // 2, 1)])
    return " ".join(sentences[: max(len(sentences) // 2, 1)])


def suitable(entry: Entry) -> bool:
    return (
        entry.section in SECTIONS
        and not entry.has_code
        and MIN_CHARS <= len(plain(entry.answer)) <= MAX_CHARS
    )


def ladder(
    entries: list[Entry], *, questions: int = QUESTIONS, seed: int = 1
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    pool = [e for e in entries if suitable(e)]
    chosen = rng.sample(pool, questions)
    items: list[dict[str, Any]] = []
    for n, entry in enumerate(chosen, start=1):
        reference = plain(entry.answer)
        others = [e for e in pool if e.subsection != entry.subsection]
        other = plain(rng.choice(others).answer)
        variants = [
            ("full", reference, 3),
            ("half", first_half(reference), 2),
            ("other", other, 0),
            ("dont_know", DONT_KNOW, 0),
        ]
        for kind, answer, level in variants:
            items.append(
                {
                    "external_id": f"q{n}.{kind}",
                    "question_id": f"q{n}",
                    "question": entry.question,
                    "reference_answer": reference,
                    "answer": answer,
                    "human_scores": {"level": level},
                }
            )
    return items


def main() -> None:
    if not RAW.exists():
        RAW.parent.mkdir(parents=True, exist_ok=True)
        RAW.write_text(httpx.get(URL, timeout=60).raise_for_status().text, encoding="utf-8")
    entries = parse_bank(RAW.read_text(encoding="utf-8"))
    items = ladder(entries)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(dump_jsonl(META, items), encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {len(items)} ответов, вопросов {len(items) // 4}")
    print(json.dumps(sorted({i["question"] for i in items}), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
