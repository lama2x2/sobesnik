"""Живая загрузка вакансии с hh.ru. По умолчанию пропускается: pytest -m network.

Если hh.ru поменяет разметку или начнёт отклонять клиента, это будет видно здесь.
"""

from pathlib import Path

import pytest
import yaml

from sobesnik_api.sources.extract import extract_vacancy_text
from sobesnik_api.sources.fetch import PageFetcher
from sobesnik_core.limits import MIN_VACANCY_CHARS

pytestmark = pytest.mark.network

LABELS = Path(__file__).resolve().parents[3] / "research" / "generation_bench" / "labels.yaml"


async def test_hh_vacancy_by_url() -> None:
    url = yaml.safe_load(LABELS.read_text(encoding="utf-8"))["vacancies"][0]["url"]
    fetcher = PageFetcher(timeout_s=30)
    page = await fetcher.fetch(url)
    await fetcher.aclose()
    text = extract_vacancy_text(page.content, page.url, page.charset)
    assert text is not None
    assert len(text) >= MIN_VACANCY_CHARS
    assert text.startswith("Должность: ")
    assert "Описание:" in text
