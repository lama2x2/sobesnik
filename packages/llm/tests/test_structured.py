import json
import logging

import pytest
from pydantic import BaseModel, Field

from sobesnik_llm import (
    FakeProvider,
    LLMError,
    LLMOutputError,
    LLMRequest,
    LLMUnavailableError,
    clean_json_text,
    generate_structured,
    grammar_schema,
)


class Answer(BaseModel):
    items: list[str] = Field(min_length=2)


REQ = LLMRequest(system="s", prompt="дай ответ", seed=10)
GOOD = json.dumps({"items": ["a", "b"]})


async def test_valid_first_time() -> None:
    llm = FakeProvider([GOOD])
    result = await generate_structured(llm, REQ, Answer)
    assert result.value.items == ["a", "b"]
    assert len(result.attempts) == 1
    sent = llm.requests[0].json_schema
    assert sent is not None
    assert sent["properties"]["items"] == {
        "items": {"type": "string"},
        "title": "Items",
        "type": "array",
    }
    assert llm.requests[0].prompt == "дай ответ"


async def test_custom_json_schema_is_sent() -> None:
    llm = FakeProvider([GOOD])
    await generate_structured(llm, REQ, Answer, json_schema={"type": "object"})
    assert llm.requests[0].json_schema == {"type": "object"}


async def test_invalid_json_then_valid() -> None:
    llm = FakeProvider(["{not json", GOOD])
    result = await generate_structured(llm, REQ, Answer)
    assert len(result.attempts) == 2
    retry = llm.requests[1]
    assert retry.prompt.startswith("дай ответ")
    assert "не прошёл проверку" in retry.prompt
    assert retry.seed == 11


async def test_schema_violation_described_in_retry() -> None:
    llm = FakeProvider([json.dumps({"items": ["a"]}), GOOD])
    await generate_structured(llm, REQ, Answer)
    assert "items" in llm.requests[1].prompt


async def test_check_failure_retries_with_problems() -> None:
    llm = FakeProvider([json.dumps({"items": ["a", "a"]}), GOOD])

    def check(answer: Answer) -> list[str]:
        return ["пункты повторяются"] if len(set(answer.items)) < len(answer.items) else []

    result = await generate_structured(llm, REQ, Answer, check=check)
    assert result.value.items == ["a", "b"]
    assert "- пункты повторяются" in llm.requests[1].prompt


async def test_exhausted_attempts() -> None:
    llm = FakeProvider(["nope"])
    with pytest.raises(LLMOutputError) as info:
        await generate_structured(llm, REQ, Answer, max_retries=2)
    assert info.value.attempts == 3
    assert info.value.problems
    assert len(llm.requests) == 3
    assert [r.seed for r in llm.requests] == [10, 11, 12]


async def test_retry_without_seed_keeps_none() -> None:
    llm = FakeProvider(["nope", GOOD])
    await generate_structured(llm, LLMRequest(system="s", prompt="p"), Answer)
    assert llm.requests[1].seed is None


async def test_feedback_limited_to_five_problems() -> None:
    llm = FakeProvider([GOOD, GOOD])
    problems = [f"проблема {i}" for i in range(8)]
    calls = iter([problems, []])
    await generate_structured(llm, REQ, Answer, check=lambda _: next(calls))
    prompt = llm.requests[1].prompt
    assert "проблема 4" in prompt
    assert "проблема 5" not in prompt


async def test_unavailable_is_not_retried() -> None:
    llm = FakeProvider([LLMUnavailableError("down"), GOOD])
    with pytest.raises(LLMUnavailableError):
        await generate_structured(llm, REQ, Answer)
    assert len(llm.requests) == 1


async def test_llm_error_is_not_retried() -> None:
    llm = FakeProvider([LLMError("400"), GOOD])
    with pytest.raises(LLMError):
        await generate_structured(llm, REQ, Answer)
    assert len(llm.requests) == 1


async def test_think_and_fence_are_stripped() -> None:
    llm = FakeProvider([f"<think>\nхм\n</think>\n```json\n{GOOD}\n```"])
    result = await generate_structured(llm, REQ, Answer)
    assert result.value.items == ["a", "b"]


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('  {"a": 1}\n', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('<think></think>{"a": 1}', '{"a": 1}'),
    ],
)
def test_clean_json_text(raw: str, clean: str) -> None:
    assert clean_json_text(raw) == clean


async def test_log_has_no_model_text(caplog: pytest.LogCaptureFixture) -> None:
    secret = "СЕКРЕТНЫЙ_ТЕКСТ"
    llm = FakeProvider([json.dumps({"items": [secret]}), json.dumps({"items": [secret, "b"]})])
    with caplog.at_level(logging.INFO, logger="sobesnik_llm.structured"):
        await generate_structured(llm, REQ, Answer)
    assert len(caplog.records) == 2
    for record in caplog.records:
        assert secret not in json.dumps(record.__dict__, default=str, ensure_ascii=False)
    assert caplog.records[0].accepted is False  # type: ignore[attr-defined]
    assert caplog.records[1].accepted is True  # type: ignore[attr-defined]


def test_grammar_schema_drops_validator_only_keys() -> None:
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {
            "a": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"$ref": "#/$defs/B"}},
            "level": {"enum": ["x", "y"]},
        },
        "$defs": {"B": {"type": "string", "minLength": 1, "maxLength": 5, "pattern": "^a"}},
        "anyOf": [{"type": "string", "maxLength": 2}, {"type": "null"}],
    }
    loose = grammar_schema(schema)
    assert loose == {
        "type": "object",
        "required": ["a"],
        "properties": {
            "a": {"type": "array", "items": {"$ref": "#/$defs/B"}},
            "level": {"enum": ["x", "y"]},
        },
        "$defs": {"B": {"type": "string"}},
        "anyOf": [{"type": "string"}, {"type": "null"}],
    }
    assert "minItems" in str(schema)  # исходная схема не изменена
