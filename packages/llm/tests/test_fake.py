import pytest

from sobesnik_llm import FakeProvider, LLMError, LLMRequest, LLMUnavailableError

REQ = LLMRequest(system="s", prompt="p")


async def test_replies_in_order_last_repeats() -> None:
    provider = FakeProvider(["a", "b"])
    texts = [(await provider.generate(REQ)).text for _ in range(3)]
    assert texts == ["a", "b", "b"]
    assert provider.requests == [REQ, REQ, REQ]


async def test_callable_reply_sees_request() -> None:
    provider = FakeProvider([lambda r: r.prompt.upper()])
    assert (await provider.generate(REQ)).text == "P"


async def test_exception_reply_is_raised() -> None:
    provider = FakeProvider([LLMError("boom"), "ok"])
    with pytest.raises(LLMError):
        await provider.generate(REQ)
    assert (await provider.generate(REQ)).text == "ok"


async def test_unavailable() -> None:
    provider = FakeProvider(["a"])
    provider.available = False
    with pytest.raises(LLMUnavailableError):
        await provider.generate(REQ)
    assert not (await provider.health()).ok
