import json
from collections.abc import Callable

import httpx
import pytest

from sobesnik_llm import LLMError, LLMRequest, LLMUnavailableError, OllamaProvider

Handler = Callable[[httpx.Request], httpx.Response]

CHAT_OK = {
    "model": "qwen3:8b",
    "message": {"role": "assistant", "content": '{"ok": true}'},
    "prompt_eval_count": 42,
    "eval_count": 7,
}


def make(handler: Handler, *, num_ctx: int | None = None) -> OllamaProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider("http://ollama:11434/", "qwen3:8b", num_ctx=num_ctx, client=client)


async def test_generate_builds_chat_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CHAT_OK)

    provider = make(handler, num_ctx=16384)
    schema = {"type": "object"}
    response = await provider.generate(
        LLMRequest(
            system="sys", prompt="hi", json_schema=schema, temperature=0.0, seed=1, max_tokens=99
        )
    )

    assert str(seen[0].url) == "http://ollama:11434/api/chat"
    body = json.loads(seen[0].content)
    assert body["model"] == "qwen3:8b"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"] == schema
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert body["options"] == {
        "temperature": 0.0,
        "seed": 1,
        "num_ctx": 16384,
        "num_predict": 99,
    }

    assert response.text == '{"ok": true}'
    assert response.provider == "ollama"
    assert (response.tokens_in, response.tokens_out) == (42, 7)


async def test_generate_without_schema_omits_format() -> None:
    provider = make(lambda _: httpx.Response(200, json=CHAT_OK))
    payload = provider.build_payload(LLMRequest(system="s", prompt="p"))
    assert "format" not in payload
    assert payload["options"] == {"temperature": 0.2}


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda _: httpx.Response(404, json={"error": "not found"}), id="no-model"),
        pytest.param(lambda _: httpx.Response(503), id="5xx"),
    ],
)
async def test_generate_unavailable_statuses(handler: Handler) -> None:
    with pytest.raises(LLMUnavailableError):
        await make(handler).generate(LLMRequest(system="s", prompt="p"))


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["connect", "timeout"],
)
async def test_generate_transport_errors(exc: Exception) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(LLMUnavailableError):
        await make(handler).generate(LLMRequest(system="s", prompt="p"))


async def test_generate_bad_request_is_llm_error() -> None:
    provider = make(lambda _: httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(LLMError) as info:
        await provider.generate(LLMRequest(system="s", prompt="p"))
    assert not isinstance(info.value, LLMUnavailableError)


async def test_generate_malformed_body() -> None:
    provider = make(lambda _: httpx.Response(200, json={"unexpected": 1}))
    with pytest.raises(LLMError):
        await provider.generate(LLMRequest(system="s", prompt="p"))


async def test_health_ok() -> None:
    tags = {"models": [{"name": "qwen3:8b"}, {"name": "other:latest"}]}
    health = await make(lambda _: httpx.Response(200, json=tags)).health()
    assert health.ok
    assert health.detail is None


async def test_health_model_missing() -> None:
    health = await make(lambda _: httpx.Response(200, json={"models": []})).health()
    assert not health.ok
    assert health.detail is not None
    assert "не скачана" in health.detail


async def test_health_latest_tag_matches_bare_name() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"models": [{"name": "llama3:latest"}]})
        )
    )
    provider = OllamaProvider("http://ollama:11434", "llama3", client=client)
    assert (await provider.health()).ok


async def test_health_unreachable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    health = await make(handler).health()
    assert not health.ok
    assert health.detail is not None
    assert "недоступна" in health.detail
