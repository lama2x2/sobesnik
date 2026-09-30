from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sobesnik_api.topics_sync import sync_topics
from sobesnik_core.topics import TopicCatalog, load_topics, parse_catalog


async def test_sync_is_idempotent_and_keeps_removed(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession)
    catalog = load_topics()
    async with sessionmaker() as db:
        await sync_topics(db, catalog)
        await sync_topics(db, catalog)
    async with engine.connect() as conn:
        count = await conn.scalar(text("SELECT count(*) FROM topic"))
        parent = await conn.scalar(text("SELECT parent_slug FROM topic WHERE slug = 'db.indexes'"))
    assert count == len(catalog.all())
    assert parent == "db"

    smaller: TopicCatalog = parse_catalog(
        {
            "areas": [
                {
                    "slug": "db",
                    "title": "БД (новое имя)",
                    "topics": [{"slug": "db.general", "title": "Общее"}],
                }
            ]
        }
    )
    async with sessionmaker() as db:
        await sync_topics(db, smaller)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM topic")) == count
        title = await conn.scalar(text("SELECT title FROM topic WHERE slug = 'db'"))
    assert title == "БД (новое имя)"
