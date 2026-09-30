"""Сборка приложения FastAPI. Без побочных эффектов при импорте — его используют тесты."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from sobesnik_api.db.session import make_engine, make_sessionmaker
from sobesnik_api.errors import install_error_handlers
from sobesnik_api.routes import health, sessions, users, vacancies
from sobesnik_api.settings import Settings
from sobesnik_llm import LLMProvider, make_provider


def create_app(settings: Settings | None = None, llm: LLMProvider | None = None) -> FastAPI:
    """`llm` передают тесты; без него провайдер выбирается по env."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.sessionmaker = make_sessionmaker(engine)
        provider = llm or make_provider(
            settings.llm_provider,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_s=settings.llm_timeout_s,
            num_ctx=settings.llm_num_ctx,
        )
        app.state.llm = provider
        yield
        await provider.aclose()
        await engine.dispose()

    app = FastAPI(title="Sobesnik API", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    api = APIRouter(prefix="/api/v1")
    for module in (health, users, vacancies, sessions):
        api.include_router(module.router)
    app.include_router(api)
    return app
