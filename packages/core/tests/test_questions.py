import pytest
from pydantic import ValidationError

from sobesnik_core.questions import GeneratedQuestions, questions_json_schema

KP = ["a", "b", "c"]


def test_schema_fixes_count() -> None:
    schema = questions_json_schema(7)
    assert schema["properties"]["questions"]["minItems"] == 7
    assert schema["properties"]["questions"]["maxItems"] == 7


def test_schema_does_not_mutate_model_schema() -> None:
    questions_json_schema(7)
    assert GeneratedQuestions.model_json_schema()["properties"]["questions"]["minItems"] == 1


def test_schema_key_points_bounds() -> None:
    item = GeneratedQuestions.model_json_schema()["$defs"]["GeneratedQuestion"]
    key_points = item["properties"]["key_points"]
    assert (key_points["minItems"], key_points["maxItems"]) == (3, 6)
    assert "requirement_index" not in item["properties"]


def test_valid() -> None:
    parsed = GeneratedQuestions.model_validate(
        {"questions": [{"text": "Что такое GIL?", "key_points": [" a ", "b", "c"]}]}
    )
    assert parsed.questions[0].key_points == KP


@pytest.mark.parametrize(
    "item",
    [
        {"text": "Q", "key_points": ["a", "b"]},
        {"text": "Q", "key_points": ["a"] * 7},
        {"text": "Q", "key_points": ["a", "b", " "]},
        {"text": "Q"},
        {"text": "", "key_points": KP},
        {"text": "Q", "key_points": KP, "requirement_index": 0},
    ],
)
def test_invalid(item: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GeneratedQuestions.model_validate({"questions": [item]})


def test_empty_questions_rejected() -> None:
    with pytest.raises(ValidationError):
        GeneratedQuestions.model_validate({"questions": []})
