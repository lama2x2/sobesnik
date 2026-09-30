"""Настройки из env. Значения по умолчанию подходят для compose на Mac с Ollama на хосте."""

from pydantic_settings import BaseSettings, SettingsConfigDict

from sobesnik_llm import ProviderName


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+asyncpg://sobesnik:sobesnik@postgres:5432/sobesnik"

    llm_provider: ProviderName = "ollama"
    llm_base_url: str = "http://host.docker.internal:11434"
    llm_model: str = "qwen3:8b"
    llm_timeout_s: float = 120.0
    llm_num_ctx: int | None = 16384

    log_level: str = "INFO"
