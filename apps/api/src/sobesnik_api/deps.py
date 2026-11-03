"""Зависимости FastAPI: сессия БД, провайдеры LLM, очередь, хранилище аудио, загрузчик страниц
и настройки."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.asr_status import ASRStatus
from sobesnik_api.llm import LLMRoles
from sobesnik_api.queue import TaskQueue
from sobesnik_api.settings import Settings
from sobesnik_api.sources.fetch import PageFetcher
from sobesnik_asr import AudioStorage
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


def get_queue(request: Request) -> TaskQueue:
    queue: TaskQueue = request.app.state.queue
    return queue


def get_storage(request: Request) -> AudioStorage:
    storage: AudioStorage = request.app.state.storage
    return storage


def get_asr_status(request: Request) -> ASRStatus:
    status: ASRStatus = request.app.state.asr_status
    return status


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


DB = Annotated[AsyncSession, Depends(get_db)]
LLM = Annotated[LLMProvider, Depends(get_llm_gen)]
Roles = Annotated[LLMRoles, Depends(get_roles)]
Fetcher = Annotated[PageFetcher, Depends(get_fetcher)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Queue = Annotated[TaskQueue, Depends(get_queue)]
Storage = Annotated[AudioStorage, Depends(get_storage)]
ASRStatusDep = Annotated[ASRStatus, Depends(get_asr_status)]
