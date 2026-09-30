import pytest
from pydantic import SecretStr

from sobesnik_api.llm import make_roles
from sobesnik_api.settings import Settings
from sobesnik_llm import OllamaProvider, OpenAICompatibleProvider


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """В tools задано LLM_PROVIDER=fake; тесты настроек от env не зависят."""
    import os

    for name in list(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)


def test_roles_inherit_base() -> None:
    settings = Settings(llm_provider="ollama", llm_model="base")
    assert settings.llm_config("gen") == settings.llm_config("eval")
    assert settings.llm_config("gen").model == "base"


def test_eval_model_override_keeps_base() -> None:
    settings = Settings(llm_base_url="http://o:11434", llm_model="base", llm_eval_model="judge")
    gen, eval_ = settings.llm_config("gen"), settings.llm_config("eval")
    assert (gen.model, eval_.model) == ("base", "judge")
    assert gen.base_url == eval_.base_url == "http://o:11434"
    assert gen.provider == eval_.provider == "ollama"


def test_full_override() -> None:
    settings = Settings(
        llm_gen_provider="openai_compatible",
        llm_gen_base_url="https://cloud/v1",
        llm_gen_api_key=SecretStr("key"),
        llm_gen_model="cloud-model",
        llm_structured="json_object",
    )
    gen = settings.llm_config("gen")
    assert gen.provider == "openai_compatible"
    assert gen.api_key is not None
    assert gen.api_key.get_secret_value() == "key"
    assert gen.structured == "json_object"
    assert settings.llm_config("eval").provider == "ollama"


def test_empty_env_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_GEN_MODEL", "")
    monkeypatch.setenv("LLM_EVAL_PROVIDER", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "base")
    settings = Settings()
    assert settings.llm_config("gen").model == "base"
    assert settings.llm_config("eval").provider == "ollama"
    assert settings.llm_api_key is None


async def test_make_roles_shares_identical_provider() -> None:
    roles = make_roles(Settings())
    assert roles.gen is roles.eval
    assert isinstance(roles.gen, OllamaProvider)
    await roles.aclose()


async def test_make_roles_separate_providers() -> None:
    roles = make_roles(Settings(llm_eval_provider="openai_compatible", llm_eval_model="judge"))
    assert isinstance(roles.gen, OllamaProvider)
    assert isinstance(roles.eval, OpenAICompatibleProvider)
    assert roles.get("eval").model == "judge"
    await roles.aclose()
