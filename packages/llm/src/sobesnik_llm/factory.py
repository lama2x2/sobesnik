"""Выбор провайдера по настройкам."""

from sobesnik_llm.base import LLMProvider
from sobesnik_llm.config import LLMConfig
from sobesnik_llm.fake import FakeProvider
from sobesnik_llm.ollama import OllamaProvider
from sobesnik_llm.openai_compatible import OpenAICompatibleProvider


def make_provider(config: LLMConfig) -> LLMProvider:
    match config.provider:
        case "ollama":
            return OllamaProvider(
                config.base_url,
                config.model,
                timeout_s=config.timeout_s,
                num_ctx=config.num_ctx,
            )
        case "openai_compatible":
            api_key = config.api_key.get_secret_value() if config.api_key else None
            return OpenAICompatibleProvider(
                config.base_url,
                config.model,
                api_key=api_key,
                timeout_s=config.timeout_s,
                structured=config.structured,
            )
        case "fake":
            return FakeProvider(model=config.model)
    raise ValueError(f"неизвестный провайдер LLM: {config.provider}")
