"""Слой провайдеров LLM: единый интерфейс, выбор провайдера через env."""

from sobesnik_llm.base import (
    LLMError,
    LLMHealth,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
)
from sobesnik_llm.factory import ProviderName, make_provider
from sobesnik_llm.fake import FakeProvider
from sobesnik_llm.ollama import OllamaProvider

__all__ = [
    "FakeProvider",
    "LLMError",
    "LLMHealth",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMUnavailableError",
    "OllamaProvider",
    "ProviderName",
    "make_provider",
]
