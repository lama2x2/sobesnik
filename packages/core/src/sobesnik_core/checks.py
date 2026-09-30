"""Доменные проверки сгенерированных вопросов (002 §9.3)."""

import re
from collections.abc import Iterable, Sequence

from sobesnik_core.questions import GeneratedQuestion

# Вопрос о личном опыте, проектах или предпочтениях кандидата: на него нельзя поставить балл
# по рубрике. Шаблоны применяются к нормализованному тексту (нижний регистр, ё → е).
# Вопросы-сценарии («как бы вы решили…») проходят.
EXPERIENCE_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"\b(ваш|тво|сво)\w*\s+(\w+\s+)?опыт",
        r"\bу (тебя|вас)\b.{0,40}\bопыт",
        r"\b(приходил|доводил|сталкивал|использовал|работал|применял)\w*\s+ли\b",
        r"\b(был|была|было|были|есть)\s+ли\s+у\s+(тебя|вас)\b",
        r"\bв\s+(тво|ваш|сво)\w*\s+(\w+\s+)?(проект|работ|команд|компани)",
        r"\b(ты|вы)\s+(уже\s+)?(работал|использовал|применял|сталкивал|внедрял|настраивал"
        r"|разрабатывал|решал|делал|писал)\w*",
        r"\bкак\s+(ты|вы)\s+обычно\b",
        r"\b(ты|вы)\s+(предпочита|люб)\w*",
        r"\bрасскаж\w*\s+(о|об|про)\s+(сво\w*\s+|ваш\w*\s+|тво\w*\s+)?(случа|проект|опыт)",
        r"\bиз\s+(ваш|тво|сво)\w*\s+практик",
    )
)


def normalize_question(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def is_experience_question(text: str) -> bool:
    normalized = normalize_question(text)
    return any(p.search(normalized) for p in EXPERIENCE_PATTERNS)


def question_problems(
    questions: Sequence[GeneratedQuestion], asked: Iterable[str] = ()
) -> list[str]:
    """Проблемы сгенерированных вопросов. Пустой список — вопросы приняты."""
    problems = []
    asked_set = {normalize_question(q) for q in asked}
    seen: dict[str, int] = {}
    for i, question in enumerate(questions):
        key = normalize_question(question.text)
        if is_experience_question(question.text):
            problems.append(
                f"questions.{i}: вопрос о личном опыте кандидата, нужен вопрос на понимание"
            )
        if key in seen:
            problems.append(f"questions.{i}: повторяет вопрос {seen[key]}")
        elif key in asked_set:
            problems.append(f"questions.{i}: этот вопрос уже задавался по вакансии")
        seen.setdefault(key, i)
    return problems
