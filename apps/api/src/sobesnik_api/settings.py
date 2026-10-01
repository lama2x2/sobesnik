"""Настройки из env. Значения по умолчанию подходят для compose на Mac с Ollama на хосте."""

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sobesnik_llm import LLMConfig, ProviderName, StructuredMode

Role = Literal["gen", "eval"]
ROLES: tuple[Role, ...] = ("gen", "eval")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+asyncpg://sobesnik:sobesnik@postgres:5432/sobesnik"

    # Базовые настройки LLM; роли gen и eval могут переопределить провайдер, адрес, ключ и модель.
    llm_provider: ProviderName = "ollama"
    llm_base_url: str = "http://host.docker.internal:11434"
    llm_api_key: SecretStr | None = None
    llm_model: str = "qwen3.5:4b"
    llm_timeout_s: float = 120.0
    llm_num_ctx: int | None = 16384
    llm_structured: StructuredMode = "json_schema"
    llm_max_retries: int = 2
    llm_reasoning_effort: str | None = None

    llm_gen_provider: ProviderName | None = None
    llm_gen_base_url: str | None = None
    llm_gen_api_key: SecretStr | None = None
    llm_gen_model: str | None = None

    llm_eval_provider: ProviderName | None = None
    llm_eval_base_url: str | None = None
    llm_eval_api_key: SecretStr | None = None
    llm_eval_model: str | None = None

    fetch_timeout_s: float = 10.0
    fetch_allow_private: bool = False

    log_level: str = "INFO"

    @field_validator(
        "llm_api_key",
        "llm_reasoning_effort",
        "llm_gen_provider",
        "llm_gen_base_url",
        "llm_gen_api_key",
        "llm_gen_model",
        "llm_eval_provider",
        "llm_eval_base_url",
        "llm_eval_api_key",
        "llm_eval_model",
        mode="before",
    )
    @classmethod
    def empty_is_none(cls, value: object) -> object:
        """`LLM_GEN_MODEL=` в .env означает «не задано», а не пустую строку."""
        return None if value == "" else value

    def llm_config(self, role: Role) -> LLMConfig:
        provider = getattr(self, f"llm_{role}_provider") or self.llm_provider
        base_url = getattr(self, f"llm_{role}_base_url") or self.llm_base_url
        api_key = getattr(self, f"llm_{role}_api_key") or self.llm_api_key
        model = getattr(self, f"llm_{role}_model") or self.llm_model
        return LLMConfig(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_s=self.llm_timeout_s,
            num_ctx=self.llm_num_ctx,
            structured=self.llm_structured,
            reasoning_effort=self.llm_reasoning_effort,
        )
