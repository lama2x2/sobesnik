"""Справочник тем: ресурс пакета topics.yaml (002 §7)."""

import re
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

import yaml

SLUG_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)?$")
GENERAL = "general"


@dataclass(frozen=True)
class Topic:
    slug: str
    title: str
    parent: str | None = None


@dataclass(frozen=True)
class TopicCatalog:
    areas: tuple[Topic, ...]
    leaves: tuple[Topic, ...]
    stack_aliases: dict[str, str]

    @property
    def leaf_slugs(self) -> tuple[str, ...]:
        return tuple(t.slug for t in self.leaves)

    def is_leaf(self, slug: str) -> bool:
        return slug in self._leaf_set

    @property
    def _leaf_set(self) -> frozenset[str]:
        return frozenset(self.leaf_slugs)

    def is_area(self, slug: str) -> bool:
        return any(a.slug == slug for a in self.areas)

    def resolve(self, slug: str) -> str:
        """Слаг листа как есть; выдуманный лист известной области — `<область>.general`.

        Слаг с неизвестной областью возвращается без изменений — его ловит проверка.
        """
        if self.is_leaf(slug):
            return slug
        area = slug.split(".", 1)[0]
        if self.is_area(area):
            return f"{area}.{GENERAL}"
        return slug

    def all(self) -> tuple[Topic, ...]:
        """Области раньше листов: так их можно вставлять в БД по порядку."""
        return self.areas + self.leaves

    def prompt_list(self) -> str:
        """Список листовых тем для промпта: по строке «slug — заголовок»."""
        return "\n".join(f"{t.slug} — {t.title}" for t in self.leaves)


def parse_catalog(data: dict[str, Any]) -> TopicCatalog:
    """Проверяет структуру справочника и собирает его. Ошибка — ValueError."""
    areas: list[Topic] = []
    leaves: list[Topic] = []
    seen: set[str] = set()

    def add(slug: str, title: str, parent: str | None) -> Topic:
        if not SLUG_RE.match(slug):
            raise ValueError(f"неверный slug темы: {slug!r}")
        if slug in seen:
            raise ValueError(f"slug темы повторяется: {slug}")
        if not title:
            raise ValueError(f"у темы {slug} нет заголовка")
        seen.add(slug)
        return Topic(slug=slug, title=title, parent=parent)

    for area in data["areas"]:
        area_slug = area["slug"]
        if "." in area_slug:
            raise ValueError(f"slug области не может содержать точку: {area_slug}")
        areas.append(add(area_slug, area["title"], None))
        children = [add(t["slug"], t["title"], area_slug) for t in area["topics"]]
        for child in children:
            if not child.slug.startswith(area_slug + "."):
                raise ValueError(f"тема {child.slug} не из области {area_slug}")
        if f"{area_slug}.{GENERAL}" not in {c.slug for c in children}:
            raise ValueError(f"в области {area_slug} нет темы {area_slug}.{GENERAL}")
        leaves.extend(children)

    aliases = {str(k).lower(): str(v).lower() for k, v in (data.get("stack_aliases") or {}).items()}
    return TopicCatalog(areas=tuple(areas), leaves=tuple(leaves), stack_aliases=aliases)


@cache
def load_topics() -> TopicCatalog:
    text = files("sobesnik_core").joinpath("topics.yaml").read_text(encoding="utf-8")
    return parse_catalog(yaml.safe_load(text))


DOC_TITLE_MAX = 60
DOCS_PER_TOPIC = (1, 3)


@dataclass(frozen=True)
class TopicDoc:
    """Ссылка «что почитать» (007 §9): только адрес и короткое название, текст не храним."""

    title: str
    url: str
    check: bool = True
    """`false` — сайт режет ботов, сетевой тест ссылку пропускает."""


def parse_topic_docs(
    data: dict[str, Any], catalog: TopicCatalog
) -> dict[str, tuple[TopicDoc, ...]]:
    """Проверяет карту «тема → ссылки» против справочника. Ошибка — ValueError."""
    docs: dict[str, tuple[TopicDoc, ...]] = {}
    for slug, items in data.items():
        if not catalog.is_leaf(slug):
            raise ValueError(f"ссылки для неизвестной темы: {slug}")
        low, high = DOCS_PER_TOPIC
        if not low <= len(items) <= high:
            raise ValueError(f"у темы {slug} должно быть {low}–{high} ссылки, а не {len(items)}")
        parsed = tuple(
            TopicDoc(title=str(i["title"]), url=str(i["url"]), check=bool(i.get("check", True)))
            for i in items
        )
        for doc in parsed:
            if not doc.url.startswith("https://"):
                raise ValueError(f"ссылка темы {slug} не https: {doc.url}")
            if not doc.title or len(doc.title) > DOC_TITLE_MAX:
                raise ValueError(f"название ссылки темы {slug} пустое или длиннее {DOC_TITLE_MAX}")
        if len({d.url for d in parsed}) != len(parsed):
            raise ValueError(f"у темы {slug} повторяются ссылки")
        docs[slug] = parsed
    return docs


@cache
def load_topic_docs() -> dict[str, tuple[TopicDoc, ...]]:
    text = files("sobesnik_core").joinpath("topic_docs.yaml").read_text(encoding="utf-8")
    return parse_topic_docs(yaml.safe_load(text) or {}, load_topics())


def topic_docs(slug: str | None) -> tuple[TopicDoc, ...]:
    if slug is None:
        return ()
    return load_topic_docs().get(slug, ())
