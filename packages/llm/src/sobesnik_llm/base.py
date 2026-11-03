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
    max_tokens: int | None = None
    """Лимит выходных токенов: зациклившаяся модель не должна генерировать до конца контекста."""


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
    hint: str | None = None
    """Что сделать человеку, по-русски и без технических подробностей (008 §7)."""


class LLMError(Exception):
    """Провайдер вернул ошибку, которую не исправить повтором."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint
        """Что сделать человеку (008 §7); `None` — общего текста достаточно."""


class LLMUnavailableError(LLMError):
    """LLM недоступна: нет соединения, таймаут, 5xx или модель не скачана."""


class LLMModelMissingError(LLMUnavailableError):
    """Модели нет у провайдера: в Ollama её не скачали, у облака неверное имя."""


class LLMOutputError(LLMError):
    """Ответ модели так и не прошёл проверку за все попытки."""

    def __init__(
        self, problems: list[str], *, attempts: int, responses: list[LLMResponse] | None = None
    ) -> None:
        super().__init__(f"ответ модели не прошёл проверку за {attempts} попыток")
        self.problems = problems
        self.attempts = attempts
        self.responses = responses or []
        """Все отвергнутые ответы модели: для записи неудачной оценки (003 §7)."""


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, request: LLMRequest) -> LLMResponse: ...

    async def health(self) -> LLMHealth: ...

    async def aclose(self) -> None: ...
