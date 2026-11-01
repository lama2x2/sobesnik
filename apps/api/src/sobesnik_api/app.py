"""Сборка приложения FastAPI. Без побочных эффектов при импорте — его используют тесты."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from sobesnik_api.db.session import make_engine, make_sessionmaker
from sobesnik_api.errors import install_error_handlers
from sobesnik_api.llm import LLMRoles, make_roles
from sobesnik_api.queue import ArqQueue, TaskQueue
from sobesnik_api.routes import answers, health, sessions, users, vacancies
from sobesnik_api.settings import Settings
from sobesnik_api.sources.fetch import PageFetcher
from sobesnik_api.topics_sync import sync_topics
from sobesnik_asr import AudioStorage, StorageError
from sobesnik_core.topics import load_topics
from sobesnik_llm import LLMProvider

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
    *,
    llm_eval: LLMProvider | None = None,
    fetcher: PageFetcher | None = None,
    queue: TaskQueue | None = None,
    storage: AudioStorage | None = None,
) -> FastAPI:
    """`llm`, `llm_eval`, `fetcher`, `queue` и `storage` передают тесты; без них всё собирается
    по env.

    `llm` без `llm_eval` обслуживает обе роли.
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.sessionmaker = make_sessionmaker(engine)
        async with app.state.sessionmaker() as db:
            await sync_topics(db, load_topics())
        roles = LLMRoles(gen=llm, eval=llm_eval or llm) if llm else make_roles(settings)
        app.state.llm = roles
        app.state.fetcher = fetcher or PageFetcher(
            timeout_s=settings.fetch_timeout_s, allow_private=settings.fetch_allow_private
        )
        app.state.queue = queue or ArqQueue(settings.redis_url)
        app.state.storage = storage or settings.audio_storage()
        try:
            await app.state.storage.ensure_bucket()
        except StorageError as exc:  # приём голоса ответит 503, остальное работает
            log.warning("audio bucket not ready", extra={"error": type(exc).__name__})
        app.state.settings = settings
        yield
        await app.state.queue.aclose()
        await app.state.fetcher.aclose()
        await roles.aclose()
        await engine.dispose()

    app = FastAPI(title="Sobesnik API", version="0.3.0", lifespan=lifespan)
    install_error_handlers(app)
    api = APIRouter(prefix="/api/v1")
    for module in (health, users, vacancies, sessions, answers):
        api.include_router(module.router)
    app.include_router(api)
    return app
