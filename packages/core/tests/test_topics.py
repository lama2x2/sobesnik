import copy
from typing import Any

import pytest

from sobesnik_core.topics import SLUG_RE, load_topics, parse_catalog

VALID: dict[str, Any] = {
    "areas": [
        {
            "slug": "db",
            "title": "БД",
            "topics": [
                {"slug": "db.sql", "title": "SQL"},
                {"slug": "db.general", "title": "БД — общее"},
            ],
        }
    ],
    "stack_aliases": {"Postgres": "PostgreSQL"},
}


def test_package_catalog_is_valid() -> None:
    catalog = load_topics()
    assert 60 <= len(catalog.leaves) <= 100
    assert 12 <= len(catalog.areas) <= 14
    slugs = [t.slug for t in catalog.all()]
    assert len(slugs) == len(set(slugs))
    assert all(SLUG_RE.match(s) for s in slugs)
    area_slugs = {a.slug for a in catalog.areas}
    for leaf in catalog.leaves:
        assert leaf.parent in area_slugs
        assert leaf.slug.startswith(f"{leaf.parent}.")
    for area in catalog.areas:
        assert catalog.is_leaf(f"{area.slug}.general")


def test_package_aliases_point_to_normalized_names() -> None:
    aliases = load_topics().stack_aliases
    assert aliases["k8s"] == "kubernetes"
    assert aliases["postgres"] == "postgresql"
    assert len(aliases) >= 30


def test_parse_valid() -> None:
    catalog = parse_catalog(VALID)
    assert catalog.leaf_slugs == ("db.sql", "db.general")
    assert catalog.areas[0].parent is None
    assert catalog.is_leaf("db.sql")
    assert not catalog.is_leaf("db")
    assert catalog.stack_aliases == {"postgres": "postgresql"}
    assert catalog.prompt_list() == "db.sql — SQL\ndb.general — БД — общее"
    assert [t.slug for t in catalog.all()] == ["db", "db.sql", "db.general"]


def broken(path: str, value: Any) -> dict[str, Any]:
    data = copy.deepcopy(VALID)
    area = data["areas"][0]
    match path:
        case "area_slug":
            area["slug"] = value
        case "topic_slug":
            area["topics"][0]["slug"] = value
        case "no_general":
            area["topics"] = [area["topics"][0]]
        case "title":
            area["topics"][0]["title"] = value
    return data


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("area_slug", "Db"),
        ("area_slug", "db.x"),
        ("topic_slug", "web.sql"),
        ("topic_slug", "db.general"),
        ("topic_slug", "db.SQL"),
        ("no_general", None),
        ("title", ""),
    ],
)
def test_parse_invalid(path: str, value: Any) -> None:
    with pytest.raises(ValueError):
        parse_catalog(broken(path, value))
