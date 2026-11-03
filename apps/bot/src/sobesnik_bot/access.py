"""Закрытый инстанс (BOT-5): чужой `telegram_id` получает отказ, в API ничего не уходит."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from sobesnik_bot import render

log = logging.getLogger(__name__)


class AllowlistMiddleware(BaseMiddleware):
    def __init__(self, allowed: frozenset[int]) -> None:
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if not self.allowed or (user is not None and user.id in self.allowed):
            return await handler(event, data)
        log.info("access denied", extra={"telegram_id": user.id if user else None})
        text = render.not_allowed(user.id if user else None)
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        return None
