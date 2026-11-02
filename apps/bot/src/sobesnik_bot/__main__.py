"""Запуск бота: `python -m sobesnik_bot` (long polling, 006 §7)."""

import asyncio
import logging
import socket
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from redis.asyncio import Redis

from sobesnik_bot.access import AllowlistMiddleware
from sobesnik_bot.api import ApiClient
from sobesnik_bot.events import EventConsumer
from sobesnik_bot.flow import Trainer
from sobesnik_bot.handlers import build_router
from sobesnik_bot.logs import configure_logging
from sobesnik_bot.settings import BotSettings
from sobesnik_bot.state import StateStore

log = logging.getLogger("sobesnik_bot")

COMMANDS = [
    BotCommand(command="new", description="новая вакансия"),
    BotCommand(command="train", description="тренировка по вакансии"),
    BotCommand(command="forget", description="удалить мои голосовые"),
    BotCommand(command="help", description="справка"),
]


def build_dispatcher(trainer: Trainer, allowed: frozenset[int]) -> Dispatcher:
    dp = Dispatcher()
    access = AllowlistMiddleware(allowed)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(build_router(trainer))
    return dp


async def run(settings: BotSettings) -> None:
    assert settings.telegram_bot_token is not None
    token = settings.telegram_bot_token.get_secret_value()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    service_token = settings.api_service_token
    api = ApiClient(settings.api_url, service_token.get_secret_value() if service_token else None)
    trainer = Trainer(bot, api, StateStore(redis), questions=settings.bot_questions)
    consumer = EventConsumer(
        redis, settings.answer_events_stream, trainer.on_event, consumer=socket.gethostname()
    )
    dp = build_dispatcher(trainer, settings.allowed_ids)
    if not settings.allowed_ids:
        log.warning("BOT_ALLOWED_IDS is empty: the bot answers anyone")
    background: list[asyncio.Task[None]] = []
    try:
        await bot.set_my_commands(COMMANDS)
        await consumer.ensure_group()
        await trainer.resume()
        background = [
            asyncio.create_task(consumer.run()),
            asyncio.create_task(trainer.reconcile_loop()),
        ]
        log.info("bot started", extra={"allowlist": len(settings.allowed_ids)})
        await dp.start_polling(bot, handle_signals=True)
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await trainer.close()
        await api.aclose()
        await redis.aclose()
        await bot.session.close()


def main() -> None:
    settings = BotSettings()
    secrets = [s.get_secret_value() for s in (settings.telegram_bot_token,) if s is not None]
    configure_logging(settings.log_level, secrets)
    if settings.telegram_bot_token is None:
        log.warning("TELEGRAM_BOT_TOKEN не задан, бот не запущен")
        sys.exit(0)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
