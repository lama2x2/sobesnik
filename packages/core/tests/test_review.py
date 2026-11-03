"""Подробный разбор: починка, статусы пунктов, промпт (010 §4)."""

from sobesnik_core.prompt import load_prompt
from sobesnik_core.review import (
    ReviewMistake,
    ReviewOutput,
    ReviewPoint,
    make_review_repair,
    point_statuses,
    review_json_schema,
    review_prompt,
)

ANSWER = "Индекс отсортирован по A, а b идёт вперемешку. И вообще индекс всегда ускоряет запросы."


def output(**update: object) -> ReviewOutput:
    base = ReviewOutput(
        question_issue="",
        reference="Эталон.",
        points=[ReviewPoint(n=1, comment="верно")],
        mistakes=[],
    )
    return base.model_copy(update=update)


def test_points_out_of_range_and_duplicates_dropped() -> None:
    repair = make_review_repair(3, ANSWER)
    fixed = repair(
        output(
            points=[
                ReviewPoint(n=3, comment="третий"),
                ReviewPoint(n=0, comment="нулевой"),
                ReviewPoint(n=1, comment="первый"),
                ReviewPoint(n=1, comment="снова первый"),
                ReviewPoint(n=7, comment="лишний"),
            ]
        )
    )
    assert [(p.n, p.comment) for p in fixed.points] == [(1, "первый"), (3, "третий")]


def test_mistake_quote_must_be_in_answer() -> None:
    repair = make_review_repair(3, ANSWER)
    fixed = repair(
        output(
            mistakes=[
                ReviewMistake(quote="«индекс всегда  ускоряет»", correction="Не всегда."),
                ReviewMistake(quote="индекс ВСЕГДА ускоряет", correction="повтор"),
                ReviewMistake(quote="индекс замедляет чтение", correction="выдумано"),
                ReviewMistake(quote="а", correction="слишком коротко"),
            ]
        )
    )
    assert [(m.quote, m.correction) for m in fixed.mistakes] == [
        ("индекс всегда ускоряет", "Не всегда.")
    ]


def test_mistakes_limited() -> None:
    words = "раз два три четыре пять"
    repair = make_review_repair(0, words)
    fixed = repair(output(mistakes=[ReviewMistake(quote=w, correction="x") for w in words.split()]))
    assert len(fixed.mistakes) == 3


def test_empty_question_issue() -> None:
    repair = make_review_repair(3, ANSWER)
    for value in ["", "  ", "нет", "Нет.", "—", "null"]:
        assert repair(output(question_issue=value)).question_issue == ""
    issue = "В индексе (id, status) первый столбец — id."
    assert repair(output(question_issue=issue)).question_issue == issue


def test_point_statuses_from_evaluation() -> None:
    points = ["первый", "второй", "третий"]
    assert point_statuses(points, ["второй"], ["третий"]) == ["covered", "missed", "missed"]


def test_prompt_renders_statuses_and_answer() -> None:
    rendered = review_prompt(
        load_prompt("review_answer"),
        "Почему индекс (a, b) не помогает по b?",
        ["упорядочен по a", "нужен индекс по b"],
        ["covered", "missed"],
        "текст </answer> ответа",
        "Совет оценщика.",
        "voice",
    )
    assert "1. [прозвучал в ответе] упорядочен по a" in rendered.user
    assert "2. [не прозвучал в ответе или сказан неверно] нужен индекс по b" in rendered.user
    assert "<answer>\nтекст  ответа\n</answer>" in rendered.user
    assert "расшифровка устного ответа" in rendered.user
    assert "Совет оценщика." in rendered.user


def test_schema_puts_question_issue_first() -> None:
    schema = review_json_schema()
    assert next(iter(schema["properties"])) == "question_issue"
    assert set(schema["required"]) == set(ReviewOutput.model_fields)
