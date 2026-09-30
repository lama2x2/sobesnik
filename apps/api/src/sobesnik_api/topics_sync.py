"""Синхронизация справочника тем из topics.yaml в таблицу topic (002 §7).

Новые темы добавляются, заголовки и родители обновляются. Темы, которых больше нет в YAML,
не удаляются: на них могут ссылаться требования, вопросы и прогресс.
"""

import logging

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Topic
from sobesnik_core.topics import TopicCatalog

log = logging.getLogger(__name__)


async def sync_topics(db: AsyncSession, catalog: TopicCatalog) -> None:
    rows = [{"slug": t.slug, "title": t.title, "parent_slug": t.parent} for t in catalog.all()]
    statement = insert(Topic).values(rows)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[Topic.slug],
            set_={
                "title": statement.excluded.title,
                "parent_slug": statement.excluded.parent_slug,
            },
        )
    )
    await db.commit()
    log.info("topics synced", extra={"topics": len(rows)})
