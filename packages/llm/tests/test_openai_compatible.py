import json
from collections.abc import Callable

import httpx
import pytest

from sobesnik_llm import (
    LLMError,
    LLMRequest,
    LLMUnavailableError,
    OpenAICompatibleProvider,
    StructuredMode,
)

Handler = Callable[[httpx.Request], httpx.Response]

COMPLETION = {
    "model": "qwen3:8b",
    "choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}],
    "usage": {"prompt_tokens": 42, "completion_tokens": 7},
}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def make(
    handler: Handler, *, api_key: str | None = "sk-test", structured: StructuredMode = "json_schema"
) -> OpenAICompatibleProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleProvider(
        "http://llm/v1/", "qwen3:8b", api_key=api_key, structured=structured, client=client
    )


async def test_generate_json_schema_mode() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=COMPLETION)

    response = await make(handler).generate(
        LLMRequest(
            system="sys", prompt="hi", json_schema=SCHEMA, temperature=0.0, seed=3, max_tokens=50
        )
    )

    request = seen[0]
    assert str(request.url) == "http://llm/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "qwen3:8b"
    assert body["stream"] is False
    assert (body["temperature"], body["seed"], body["max_tokens"]) == (0.0, 3, 50)
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": SCHEMA, "strict": False},
    }
    assert response.text == '{"ok": true}'
    assert response.provider == "openai_compatible"
    assert response.model == "qwen3:8b"
    assert (response.tokens_in, response.tokens_out) == (42, 7)


async def test_json_object_mode_puts_schema_into_system() -> None:
    provider = make(lambda _: httpx.Response(200, json=COMPLETION), structured="json_object")
    payload = provider.build_payload(LLMRequest(system="sys", prompt="p", json_schema=SCHEMA))
    assert payload["response_format"] == {"type": "json_object"}
    system = payload["messages"][0]["content"]
    assert system.startswith("sys")
    assert '"boolean"' in system


async def test_without_schema_and_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=COMPLETION)

    provider = make(handler, api_key=None)
    await provider.generate(LLMRequest(system="s", prompt="p"))
    body = json.loads(seen[0].content)
    assert "response_format" not in body
    assert "seed" not in body
    assert "max_tokens" not in body
    assert "authorization" not in seen[0].headers


async def test_missing_usage() -> None:
    data = {"choices": [{"message": {"content": "{}"}}]}
    response = await make(lambda _: httpx.Response(200, json=data)).generate(
        LLMRequest(system="s", prompt="p")
    )
    assert (response.tokens_in, response.tokens_out) == (None, None)
    assert response.model == "qwen3:8b"


@pytest.mark.parametrize("status", [404, 429, 500, 503])
async def test_unavailable_statuses(status: int) -> None:
    with pytest.raises(LLMUnavailableError):
        await make(lambda _: httpx.Response(status)).generate(LLMRequest(system="s", prompt="p"))


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_error_statuses(status: int) -> None:
    with pytest.raises(LLMError) as info:
        await make(lambda _: httpx.Response(status)).generate(LLMRequest(system="s", prompt="p"))
    assert not isinstance(info.value, LLMUnavailableError)
    if status != 400:
        assert "ключ" in str(info.value)


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")], ids=["connect", "timeout"]
)
async def test_transport_errors(exc: Exception) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(LLMUnavailableError):
        await make(handler).generate(LLMRequest(system="s", prompt="p"))


@pytest.mark.parametrize(
    "data", [{"choices": []}, {"choices": [{"message": {"content": None}}]}, {"x": 1}]
)
async def test_malformed_body(data: object) -> None:
    with pytest.raises(LLMError):
        await make(lambda _: httpx.Response(200, json=data)).generate(
            LLMRequest(system="s", prompt="p")
        )


def models(*ids: str) -> dict[str, object]:
    return {"data": [{"id": i} for i in ids]}


@pytest.mark.parametrize(
    ("status", "body", "ok", "detail"),
    [
        (200, models("qwen3:8b", "x"), True, None),
        (200, models("qwen3:8b:latest"), True, None),
        (200, models("other"), False, "не найдена"),
        (200, models(), True, "недоступен"),
        (404, {}, True, "недоступен"),
        (401, {}, False, "ключ"),
        (403, {}, False, "ключ"),
        (500, {}, False, "500"),
    ],
)
async def test_health(status: int, body: object, ok: bool, detail: str | None) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    health = await make(handler).health()
    assert str(seen[0].url) == "http://llm/v1/models"
    assert health.ok is ok
    if detail is None:
        assert health.detail is None
    else:
        assert health.detail is not None
        assert detail in health.detail


async def test_health_unreachable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    health = await make(handler).health()
    assert not health.ok
