from sobesnik_core.generation import format_asked, parse_prompt, questions_prompt
from sobesnik_core.planning import PlanSlot
from sobesnik_core.prompt import load_prompt
from sobesnik_core.topics import load_topics
from sobesnik_core.vacancy import VacancyProfile

PROFILE = VacancyProfile.model_validate(
    {
        "title": "Python-разработчик",
        "level": "middle",
        "stack": ["python", "postgresql"],
        "requirements": [
            {"text": "Asyncio", "is_required": True, "topic": "python.asyncio"},
            {"text": "Индексы PostgreSQL", "is_required": False, "topic": "db.indexes"},
        ],
    }
)


def test_parse_prompt_contains_topics_and_text() -> None:
    rendered = parse_prompt(load_prompt("parse_vacancy"), load_topics(), "Ищем $ разработчика")
    assert "db.indexes — Индексы и планы запросов" in rendered.system
    assert "$topics" not in rendered.system
    assert rendered.user.strip().endswith("Ищем $ разработчика")


def test_questions_prompt() -> None:
    slots = [PlanSlot(0, 1), PlanSlot(1, 1), PlanSlot(0, 2)]
    rendered = questions_prompt(
        load_prompt("generate_questions"), PROFILE, slots, ["Что такое GIL?"]
    )
    user = rendered.user
    assert "Уровень: middle" in user
    assert '["python", "postgresql"]' in user
    assert "3 вопросов" in user
    assert "1. [обязательно] Asyncio (тема: python.asyncio)" in user
    assert "2. [желательно] Индексы PostgreSQL (тема: db.indexes)" in user
    assert "3. [обязательно, аспект 2] Asyncio" in user
    assert "- Что такое GIL?" in user


def test_format_asked_limits_and_empty() -> None:
    assert format_asked([]) == ""
    block = format_asked([f"Вопрос {i}" for i in range(40)])
    assert "Вопрос 9\n" not in block
    assert "Вопрос 10" in block
    assert "Вопрос 39" in block
