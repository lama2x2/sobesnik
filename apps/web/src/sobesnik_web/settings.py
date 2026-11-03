"""Настройки веб-кабинета из env (009 §10). API и сервисный токен — те же, что у бота."""

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    api_url: str = "http://api:8000/api/v1"
    api_service_token: SecretStr | None = None
    """Нужен вебу только для «Войти без Telegram» (009 §5.3)."""
    web_local_login: bool = True
    web_session_days: int = 30
    """Срок cookie; сам токен API продлевает с каждым заходом."""
    log_level: str = "INFO"

    @field_validator("api_service_token", mode="before")
    @classmethod
    def empty_is_none(cls, value: object) -> object:
        return None if value == "" else value
