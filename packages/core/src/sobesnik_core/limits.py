"""Лимиты входных данных (000-overview §4, 002 §8)."""

MAX_VACANCY_CHARS = 20_000
MIN_VACANCY_CHARS = 80
"""Более короткий текст отклоняется как «не вакансия» без вызова LLM."""

QUESTIONS_MIN = 3
QUESTIONS_MAX = 15
QUESTIONS_DEFAULT = 5

KEY_POINTS_MIN = 3
KEY_POINTS_MAX = 6

ASKED_QUESTIONS_IN_PROMPT = 30
"""Сколько уже заданных по вакансии вопросов передаётся в промпт генерации."""
