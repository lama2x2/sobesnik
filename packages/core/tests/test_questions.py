import pytest
from pydantic import ValidationError

from sobesnik_core.questions import GeneratedQuestions, questions_json_schema


def test_schema_fixes_count() -> None:
    schema = questions_json_schema(7)
    assert schema["properties"]["questions"]["minItems"] == 7
    assert schema["properties"]["questions"]["maxItems"] == 7


def test_schema_does_not_mutate_model_schema() -> None:
    questions_json_schema(7)
    assert GeneratedQuestions.model_json_schema()["properties"]["questions"]["minItems"] == 1


def test_requirement_index_optional() -> None:
    parsed = GeneratedQuestions.model_validate({"questions": [{"text": "Что такое GIL?"}]})
    assert parsed.questions[0].requirement_index is None


def test_empty_questions_rejected() -> None:
    with pytest.raises(ValidationError):
        GeneratedQuestions.model_validate({"questions": []})
