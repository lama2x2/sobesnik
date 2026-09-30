import pytest

from sobesnik_llm import FakeProvider, OllamaProvider, make_provider


def test_ollama() -> None:
    provider = make_provider("ollama", base_url="http://x:11434", model="m")
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "m"


def test_fake() -> None:
    assert isinstance(make_provider("fake", base_url="", model="m"), FakeProvider)


def test_unknown() -> None:
    with pytest.raises(ValueError):
        make_provider("nope", base_url="", model="m")  # type: ignore[arg-type]
