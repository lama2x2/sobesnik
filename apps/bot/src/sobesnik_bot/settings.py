"""Настройки бота из env (006 §10)."""

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    telegram_bot_token: SecretStr | None = None
    """Пусто — бот не запускается (006 §7.5)."""
    bot_allowed_ids: str = ""
    """`telegram_id` через запятую; пусто — бот открыт всем (BOT-5)."""
    api_url: str = "http://api:8000/api/v1"
    api_service_token: SecretStr | None = None
    redis_url: str = "redis://redis:6379/0"
    answer_events_stream: str = "sobesnik:answer-events"
    bot_questions: int = 5
    web_public_url: str = "http://127.0.0.1:8080"
    """Адрес веб-кабинета в ссылке входа по /web (009 §5.2)."""
    log_level: str = "INFO"

    @field_validator("telegram_bot_token", "api_service_token", mode="before")
    @classmethod
    def empty_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def allowed_ids(self) -> frozenset[int]:
        return frozenset(int(part) for part in self.bot_allowed_ids.split(",") if part.strip())
