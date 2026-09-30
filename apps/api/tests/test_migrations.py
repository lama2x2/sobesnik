from collections.abc import Awaitable, Callable

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.db.base import Base


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    def diff(conn: Connection) -> list[object]:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_downgrade_and_upgrade_again(
    run_alembic: Callable[[str, str], Awaitable[None]], engine: AsyncEngine
) -> None:
    await run_alembic("downgrade", "base")
    async with engine.connect() as conn:
        tables = await conn.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        )
    assert tables == 1  # осталась только alembic_version

    await run_alembic("upgrade", "head")
    async with engine.connect() as conn:
        version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0001"
