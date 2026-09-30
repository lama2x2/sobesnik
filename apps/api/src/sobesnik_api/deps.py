"""Зависимости FastAPI: сессия БД и провайдер LLM берутся из состояния приложения."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_llm import LLMProvider


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_llm(request: Request) -> LLMProvider:
    llm: LLMProvider = request.app.state.llm
    return llm


DB = Annotated[AsyncSession, Depends(get_db)]
LLM = Annotated[LLMProvider, Depends(get_llm)]
