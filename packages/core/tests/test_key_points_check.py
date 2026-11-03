from sobesnik_core.key_points_check import (
    KeyPointIssue,
    KeyPointsCheckOutput,
    check_prompt,
    issues_by_question,
    make_check_repair,
)
from sobesnik_core.prompt import load_prompt

QUESTIONS = [
    ("Почему индекс (a, b) не помогает поиску по b?", ["упорядочен по a", "нужен индекс по b"]),
    ("Что делает time.sleep в корутине?", ["блокирует цикл событий"]),
]


def test_prompt_lists_questions_and_points() -> None:
    rendered = check_prompt(load_prompt("check_key_points"), QUESTIONS)
    assert "Вопрос 1: Почему индекс (a, b)" in rendered.user
    assert "  2. нужен индекс по b" in rendered.user
    assert "Вопрос 2: Что делает time.sleep" in rendered.user
    assert "issues: []" in rendered.system


def test_repair_keeps_only_valid_issues() -> None:
    repair = make_check_repair([2, 1])
    output = KeyPointsCheckOutput(
        issues=[
            KeyPointIssue(q=2, n=1, issue="  Неверно:   не блокирует.  "),
            KeyPointIssue(q=1, n=2, issue="Неверно: индекс по b не нужен."),
            KeyPointIssue(q=1, n=2, issue="повтор того же пункта"),
            KeyPointIssue(q=2, n=2, issue="пункта нет"),
            KeyPointIssue(q=3, n=1, issue="вопроса нет"),
            KeyPointIssue(q=1, n=1, issue="нет"),
        ]
    )
    fixed = repair(output)
    assert [(i.q, i.n, i.issue) for i in fixed.issues] == [
        (1, 2, "Неверно: индекс по b не нужен."),
        (2, 1, "Неверно: не блокирует."),
    ]
    assert issues_by_question(fixed, 3) == [
        [{"n": 2, "issue": "Неверно: индекс по b не нужен."}],
        [{"n": 1, "issue": "Неверно: не блокирует."}],
        [],
    ]
