"""Живой прогон на настоящей Ollama. По умолчанию пропускается: pytest -m ollama."""

import os

import pytest

from sobesnik_api import prompts
from sobesnik_core.questions import GeneratedQuestions, questions_json_schema
from sobesnik_core.vacancy import VacancyProfile
from sobesnik_llm import LLMRequest, OllamaProvider

pytestmark = pytest.mark.ollama

VACANCY = """\
Middle Python-разработчик в команду платежей.
Требования: опыт коммерческой разработки на Python от 2 лет, FastAPI или Django,
PostgreSQL (индексы, транзакции), асинхронное программирование (asyncio),
Docker. Будет плюсом: Kafka, Kubernetes.
"""


@pytest.fixture
def provider() -> OllamaProvider:
    return OllamaProvider(
        os.environ.get("LLM_BASE_URL", "http://host.docker.internal:11434"),
        os.environ.get("LLM_MODEL", "qwen3:8b"),
        timeout_s=300,
        num_ctx=16384,
    )


async def test_parse_and_generate(provider: OllamaProvider) -> None:
    assert (await provider.health()).ok

    response = await provider.generate(
        LLMRequest(
            system=prompts.PARSE_VACANCY_SYSTEM,
            prompt=prompts.parse_vacancy_prompt(VACANCY),
            json_schema=VacancyProfile.model_json_schema(),
            temperature=0.1,
        )
    )
    profile = VacancyProfile.model_validate_json(response.text)
    assert "python" in [s.lower() for s in profile.stack]

    response = await provider.generate(
        LLMRequest(
            system=prompts.GENERATE_QUESTIONS_SYSTEM,
            prompt=prompts.generate_questions_prompt(profile, 5),
            json_schema=questions_json_schema(5),
        )
    )
    assert len(GeneratedQuestions.model_validate_json(response.text).questions) >= 5
    await provider.aclose()
