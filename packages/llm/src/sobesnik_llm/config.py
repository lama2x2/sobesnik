"""Настройки одного провайдера. Откуда они берутся (env, тесты), пакет не знает."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr

ProviderName = Literal["ollama", "openai_compatible", "fake"]
StructuredMode = Literal["json_schema", "json_object"]


class LLMConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: ProviderName
    base_url: str
    model: str
    api_key: SecretStr | None = None
    timeout_s: float = 120.0
    num_ctx: int | None = None
    """Только для ollama."""
    structured: StructuredMode = "json_schema"
    """Только для openai_compatible."""
