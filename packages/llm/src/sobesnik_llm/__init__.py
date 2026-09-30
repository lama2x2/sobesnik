"""Слой провайдеров LLM: единый интерфейс, выбор провайдера через env."""

from sobesnik_llm.base import (
    LLMError,
    LLMHealth,
    LLMOutputError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
)
from sobesnik_llm.config import LLMConfig, ProviderName, StructuredMode
from sobesnik_llm.factory import make_provider
from sobesnik_llm.fake import FakeProvider
from sobesnik_llm.ollama import OllamaProvider
from sobesnik_llm.openai_compatible import OpenAICompatibleProvider
from sobesnik_llm.structured import (
    StructuredResult,
    clean_json_text,
    generate_structured,
    grammar_schema,
)

__all__ = [
    "FakeProvider",
    "LLMConfig",
    "LLMError",
    "LLMHealth",
    "LLMOutputError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMUnavailableError",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderName",
    "StructuredMode",
    "StructuredResult",
    "clean_json_text",
    "generate_structured",
    "grammar_schema",
    "make_provider",
]
