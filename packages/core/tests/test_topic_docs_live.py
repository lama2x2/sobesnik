"""Живая проверка ссылок «что почитать» (007 §9). По умолчанию пропускается: pytest -m network.

Падает одним сообщением со списком всех битых ссылок.
"""

import asyncio
from urllib.parse import urlsplit

import httpx
import pytest

from sobesnik_core.topics import TopicDoc, load_topic_docs

pytestmark = pytest.mark.network

USER_AGENT = "Mozilla/5.0 (compatible; sobesnik-linkcheck; +https://github.com/)"
PARALLEL = 8


def _site(host: str) -> str:
    return ".".join(host.lower().split(".")[-2:])


async def _problem(client: httpx.AsyncClient, doc: TopicDoc) -> str | None:
    try:
        response = await client.get(doc.url)
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}"
    if response.status_code >= 400:
        return f"HTTP {response.status_code}"
    start, final = urlsplit(doc.url), response.url
    if _site(start.hostname or "") != _site(final.host):
        return f"увела на другой сайт: {final}"
    if final.path in ("", "/") and start.path not in ("", "/"):
        return f"увела на главную: {final}"
    return None


async def test_topic_docs_alive() -> None:
    docs = [(slug, d) for slug, items in load_topic_docs().items() for d in items if d.check]
    limit = asyncio.Semaphore(PARALLEL)

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=15, headers={"User-Agent": USER_AGENT}
    ) as client:

        async def one(doc: TopicDoc) -> str | None:
            async with limit:
                return await _problem(client, doc)

        problems = await asyncio.gather(*(one(d) for _, d in docs))
    broken = [f"{slug}: {d.url} — {p}" for (slug, d), p in zip(docs, problems, strict=True) if p]
    assert not broken, f"битые ссылки ({len(broken)}):\n" + "\n".join(broken)
