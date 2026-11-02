"""Настройки из env. Значения по умолчанию подходят для compose на Mac с Ollama на хосте."""

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sobesnik_asr import ASRConfig, ASRProviderName, AudioStorage, S3AudioStorage
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

    # Очередь и воркер (003 §8, §13)
    redis_url: str = "redis://redis:6379/0"
    worker_max_jobs: int = 1
    eval_max_tries: int = 5

    # Хранилище аудио (004 §5.3, §11)
    s3_endpoint_url: str = "http://s3:9000"
    s3_access_key: str = "sobesnik"
    s3_secret_key: SecretStr = SecretStr("sobesnik-secret")
    s3_bucket: str = "sobesnik-audio"
    audio_ttl_days: int = 30
    audio_max_bytes: int = 25 * 1024 * 1024
    audio_max_seconds: int = 300

    # Распознавание речи (004 §5.1, §11)
    asr_provider: ASRProviderName = "faster_whisper"
    asr_model: str = "small"
    asr_language: str = "ru"
    """`auto` — автоопределение языка."""
    asr_compute_type: str = "int8"
    asr_beam_size: int = 5
    asr_cpu_threads: int = 0
    asr_model_dir: str | None = "/models"
    asr_max_tries: int = 4

    # Доступ к API (006 §5): пусто — проверки нет / служебные эндпоинты выключены
    api_service_token: SecretStr | None = None
    admin_token: SecretStr | None = None

    # События смены статуса ответа для бота (006 §6)
    answer_events_stream: str = "sobesnik:answer-events"

    fetch_timeout_s: float = 10.0
    fetch_allow_private: bool = False
    fetch_allow_networks: str = ""
    """Подсети через запятую, которые считаются внешними: fake-IP VPN (006 §4.6)."""

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
        "api_service_token",
        "admin_token",
        mode="before",
    )
    @classmethod
    def empty_is_none(cls, value: object) -> object:
        """`LLM_GEN_MODEL=` или `ADMIN_TOKEN=` в .env означает «не задано», а не пустую строку."""
        return None if value == "" else value

    @property
    def asr_language_code(self) -> str | None:
        return None if self.asr_language in ("", "auto") else self.asr_language

    def asr_config(self) -> ASRConfig:
        return ASRConfig(
            provider=self.asr_provider,
            model=self.asr_model,
            model_dir=self.asr_model_dir,
            compute_type=self.asr_compute_type,
            beam_size=self.asr_beam_size,
            cpu_threads=self.asr_cpu_threads,
        )

    def audio_storage(self) -> AudioStorage:
        return S3AudioStorage(
            self.s3_endpoint_url,
            self.s3_access_key,
            self.s3_secret_key.get_secret_value(),
            self.s3_bucket,
        )

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
