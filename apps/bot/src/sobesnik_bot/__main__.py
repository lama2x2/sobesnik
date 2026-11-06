"""Запуск бота: `python -m sobesnik_bot` (long polling, 006 §7)."""

import asyncio
import logging
import socket
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand, User
from redis.asyncio import Redis

from sobesnik_bot.access import AllowlistMiddleware
from sobesnik_bot.api import Api, ApiClient, ApiError
from sobesnik_bot.events import EventConsumer
from sobesnik_bot.flow import Trainer
from sobesnik_bot.handlers import build_router
from sobesnik_bot.logs import NO_TELEGRAM, configure_logging
from sobesnik_bot.settings import BotSettings
from sobesnik_bot.state import StateStore

log = logging.getLogger("sobesnik_bot")

NO_TOKEN = (
    "Бот не запущен: в .env пустой TELEGRAM_BOT_TOKEN. Получите токен у @BotFather (/newbot), "
    "впишите его в .env и выполните: docker compose up -d"
)
TOKEN_REJECTED = (
    "Бот не запущен: Telegram не принял токен. Скопируйте его заново у @BotFather "
    "(/mybots → API Token), впишите в TELEGRAM_BOT_TOKEN в .env и выполните: docker compose up -d"
)
CONNECT_RETRY_S = 30.0
DNS_CACHE_S = 60

COMMANDS = [
    BotCommand(command="new", description="новая вакансия"),
    BotCommand(command="train", description="тренировка по вакансии"),
    BotCommand(command="progress", description="прогресс по темам"),
    BotCommand(command="repeat", description="повторить слабые темы"),
    BotCommand(command="forget", description="удалить мои данные"),
    BotCommand(command="web", description="войти в веб-кабинет"),
    BotCommand(command="help", description="справка"),
]


def build_dispatcher(trainer: Trainer, allowed: frozenset[int]) -> Dispatcher:
    dp = Dispatcher()
    access = AllowlistMiddleware(allowed)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(build_router(trainer))
    return dp


def telegram_session() -> AiohttpSession:
    """Короткий кэш DNS: aiogram держит адрес час, и после переключения VPN (fake-IP отдаёт
    свои адреса) бот час стучался бы на мёртвый адрес, хотя сеть уже есть."""
    session = AiohttpSession()
    session._connector_init["ttl_dns_cache"] = DNS_CACHE_S
    return session


async def connect(bot: Bot, retry_s: float = CONNECT_RETRY_S) -> User | None:
    """Проверяет токен до старта (008 §7). Неверный — `None`: бот выходит с кодом 0, без
    перезапусков по кругу; нет сети — ждёт и повторяет, без трейсбека."""
    while True:
        try:
            return await bot.get_me()
        except TelegramUnauthorizedError:
            log.error(TOKEN_REJECTED)
            return None
        except TelegramNetworkError as exc:
            log.warning(NO_TELEGRAM, extra={"error": type(exc).__name__, "retry_s": retry_s})
            await asyncio.sleep(retry_s)


async def llm_summary(api: Api) -> str:
    """Для строки «бот запущен»: модель готова или что сделать."""
    try:
        status = await api.llm_status()
    except ApiError:
        return "состояние модели неизвестно: API не ответил"
    if status.ok:
        return f"модель {status.model} готова"
    return status.hint or f"модель {status.model} недоступна"


async def run(settings: BotSettings) -> None:
    assert settings.telegram_bot_token is not None
    token = settings.telegram_bot_token.get_secret_value()
    bot = Bot(
        token,
        session=telegram_session(),
        # превью ссылок «что почитать» раздувают сообщение (007 §8.4)
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    service_token = settings.api_service_token
    api = ApiClient(settings.api_url, service_token.get_secret_value() if service_token else None)
    trainer = Trainer(
        bot,
        api,
        StateStore(redis),
        questions=settings.bot_questions,
        web_url=settings.web_public_url,
    )
    consumer = EventConsumer(
        redis, settings.answer_events_stream, trainer.on_event, consumer=socket.gethostname()
    )
    dp = build_dispatcher(trainer, settings.allowed_ids)
    if not settings.allowed_ids:
        log.warning("BOT_ALLOWED_IDS is empty: the bot answers anyone")
    background: list[asyncio.Task[None]] = []
    try:
        me = await connect(bot)
        if me is None:
            return
        await bot.set_my_commands(COMMANDS)
        await consumer.ensure_group()
        await trainer.resume()
        background = [
            asyncio.create_task(consumer.run()),
            asyncio.create_task(trainer.reconcile_loop()),
        ]
        log.info(
            f"Бот запущен: @{me.username}; {await llm_summary(api)}",
            extra={"allowlist": len(settings.allowed_ids)},
        )
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
        log.warning(NO_TOKEN)
        sys.exit(0)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
