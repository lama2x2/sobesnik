"""Живой прогон на настоящей Ollama. По умолчанию пропускается: pytest -m ollama."""

import os

import pytest

from sobesnik_api.errors import UnprocessableError
from sobesnik_api.services.vacancies import parse_vacancy
from sobesnik_core.checks import is_experience_question
from sobesnik_core.generation import questions_prompt
from sobesnik_core.planning import PlanRequirement, plan_questions
from sobesnik_core.prompt import load_prompt
from sobesnik_core.questions import GeneratedQuestions, questions_json_schema
from sobesnik_core.topics import load_topics
from sobesnik_llm import (
    LLMConfig,
    LLMProvider,
    LLMRequest,
    ProviderName,
    generate_structured,
    make_provider,
)

pytestmark = pytest.mark.ollama

OLLAMA = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:11434").removesuffix("/v1")
MODEL = os.environ.get("LLM_MODEL", "qwen3.5:4b")

VACANCY = """\
Middle Python-разработчик в команду платежей.
Требования: опыт коммерческой разработки на Python от 2 лет, FastAPI или Django,
PostgreSQL (индексы, транзакции), асинхронное программирование (asyncio),
Docker. Будет плюсом: Kafka, Kubernetes.
"""

RESUME = """\
Иван, Python-разработчик, 4 года опыта. Стек: Django, PostgreSQL, Celery, Redis.
Делал платёжный сервис и CRM. Ищу удалённую работу, открыт к предложениям.
"""


def provider(kind: ProviderName) -> LLMProvider:
    base_url = OLLAMA + "/v1" if kind == "openai_compatible" else OLLAMA
    config = LLMConfig(provider=kind, base_url=base_url, model=MODEL, timeout_s=300, num_ctx=16384)
    return make_provider(config)


@pytest.mark.parametrize("kind", ["ollama", "openai_compatible"])
async def test_parse_and_generate(kind: ProviderName) -> None:
    llm = provider(kind)
    assert (await llm.health()).ok

    parsed = await parse_vacancy(llm, VACANCY)
    profile = parsed.profile
    assert "python" in profile.stack
    catalog = load_topics()
    assert all(r.topic and catalog.is_leaf(r.topic) for r in profile.requirements)
    assert any(not r.is_required for r in profile.requirements)

    slots = plan_questions(
        [PlanRequirement(r.is_required, r.topic) for r in profile.requirements], 5
    )
    template = load_prompt("generate_questions")
    rendered = questions_prompt(template, profile, slots)
    result = await generate_structured(
        llm,
        LLMRequest(system=rendered.system, prompt=rendered.user, temperature=template.temperature),
        GeneratedQuestions,
        json_schema=questions_json_schema(5),
    )
    questions = result.value.questions
    assert len(questions) == 5
    assert all(3 <= len(q.key_points) <= 6 for q in questions)
    assert not any(is_experience_question(q.text) for q in questions)
    await llm.aclose()


async def test_resume_rejected() -> None:
    llm = provider("ollama")
    with pytest.raises(UnprocessableError) as info:
        await parse_vacancy(llm, RESUME)
    assert info.value.code == "not_a_vacancy"
    await llm.aclose()
