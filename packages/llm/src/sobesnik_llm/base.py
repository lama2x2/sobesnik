"""Единый интерфейс провайдеров LLM."""

from typing import Any, Protocol

from pydantic import BaseModel


class LLMRequest(BaseModel):
    system: str
    prompt: str
    json_schema: dict[str, Any] | None = None
    """Схема structured output. Провайдер, который её не поддерживает, может её игнорировать."""
    temperature: float = 0.2
    seed: int | None = None


class LLMResponse(BaseModel):
    text: str
    """Сырой ответ модели."""
    provider: str
    model: str
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None


class LLMHealth(BaseModel):
    ok: bool
    provider: str
    model: str
    detail: str | None = None


class LLMError(Exception):
    """Провайдер вернул ошибку, которую не исправить повтором."""


class LLMUnavailableError(LLMError):
    """LLM недоступна: нет соединения, таймаут, 5xx или модель не скачана."""


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, request: LLMRequest) -> LLMResponse: ...

    async def health(self) -> LLMHealth: ...

    async def aclose(self) -> None: ...
