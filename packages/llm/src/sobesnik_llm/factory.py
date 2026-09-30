"""Выбор провайдера по настройкам из env."""

from typing import Literal

from sobesnik_llm.base import LLMProvider
from sobesnik_llm.fake import FakeProvider
from sobesnik_llm.ollama import OllamaProvider

ProviderName = Literal["ollama", "fake"]


def make_provider(
    provider: ProviderName,
    *,
    base_url: str,
    model: str,
    timeout_s: float = 120.0,
    num_ctx: int | None = None,
) -> LLMProvider:
    match provider:
        case "ollama":
            return OllamaProvider(base_url, model, timeout_s=timeout_s, num_ctx=num_ctx)
        case "fake":
            return FakeProvider(model=model)
    raise ValueError(f"неизвестный провайдер LLM: {provider}")
