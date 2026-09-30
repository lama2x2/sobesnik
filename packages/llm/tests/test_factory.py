from pydantic import SecretStr

from sobesnik_llm import (
    FakeProvider,
    LLMConfig,
    OllamaProvider,
    OpenAICompatibleProvider,
    make_provider,
)


def test_ollama() -> None:
    provider = make_provider(LLMConfig(provider="ollama", base_url="http://x:11434", model="m"))
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "m"


def test_openai_compatible() -> None:
    config = LLMConfig(
        provider="openai_compatible",
        base_url="http://x/v1",
        model="m",
        api_key=SecretStr("secret"),
        structured="json_object",
    )
    provider = make_provider(config)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "m"


def test_fake() -> None:
    assert isinstance(
        make_provider(LLMConfig(provider="fake", base_url="", model="m")), FakeProvider
    )


def test_config_hides_key_and_compares_by_value() -> None:
    a = LLMConfig(provider="fake", base_url="", model="m", api_key=SecretStr("k"))
    b = LLMConfig(provider="fake", base_url="", model="m", api_key=SecretStr("k"))
    assert a == b
    assert "k'" not in repr(a)
