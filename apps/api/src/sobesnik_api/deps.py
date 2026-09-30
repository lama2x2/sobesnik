"""Зависимости FastAPI: сессия БД, провайдеры LLM, загрузчик страниц и настройки."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.llm import LLMRoles
from sobesnik_api.settings import Settings
from sobesnik_api.sources.fetch import PageFetcher
from sobesnik_llm import LLMProvider


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_roles(request: Request) -> LLMRoles:
    roles: LLMRoles = request.app.state.llm
    return roles


def get_llm_gen(request: Request) -> LLMProvider:
    return get_roles(request).gen


def get_fetcher(request: Request) -> PageFetcher:
    fetcher: PageFetcher = request.app.state.fetcher
    return fetcher


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


DB = Annotated[AsyncSession, Depends(get_db)]
LLM = Annotated[LLMProvider, Depends(get_llm_gen)]
Roles = Annotated[LLMRoles, Depends(get_roles)]
Fetcher = Annotated[PageFetcher, Depends(get_fetcher)]
AppSettings = Annotated[Settings, Depends(get_settings)]
