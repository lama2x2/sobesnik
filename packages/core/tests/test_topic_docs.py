from typing import Any

import pytest

from sobesnik_core.topics import (
    DOC_TITLE_MAX,
    load_topic_docs,
    load_topics,
    parse_topic_docs,
    topic_docs,
)


def test_every_leaf_has_docs() -> None:
    docs = load_topic_docs()
    leaves = set(load_topics().leaf_slugs)
    assert set(docs) == leaves
    for slug, items in docs.items():
        assert 1 <= len(items) <= 3, slug
        assert all(d.url.startswith("https://") for d in items)
        assert all(0 < len(d.title) <= DOC_TITLE_MAX for d in items)


def test_topic_docs_lookup() -> None:
    assert topic_docs("python.asyncio")[0].url.startswith("https://docs.python.org/")
    assert topic_docs(None) == ()
    assert topic_docs("nope.nope") == ()


DOC = {"title": "SQL", "url": "https://example.org/sql"}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"db.nope": [DOC]}, "неизвестной темы"),
        ({"python.asyncio": []}, "1–3"),
        ({"python.asyncio": [DOC] * 4}, "1–3"),
        ({"python.asyncio": [{"title": "x", "url": "http://example.org"}]}, "не https"),
        ({"python.asyncio": [{"title": "x" * 61, "url": "https://e.org"}]}, "длиннее"),
        ({"python.asyncio": [DOC, DOC]}, "повторяются"),
    ],
)
def test_parse_topic_docs_rejects(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_topic_docs(data, load_topics())


def test_check_flag() -> None:
    docs = parse_topic_docs({"python.asyncio": [{**DOC, "check": False}]}, load_topics())
    assert docs["python.asyncio"][0].check is False
