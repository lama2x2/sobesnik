"""Обработчики Telegram (006 §4, 007 §8): разбирают апдейт и передают его в `Trainer`."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, MessageEntity

from sobesnik_bot import render
from sobesnik_bot.flow import Trainer
from sobesnik_bot.render import (
    DetailsCb,
    ForgetCb,
    LevelCb,
    NewVacancyCb,
    ProgressCb,
    RepeatCb,
    SkipCb,
    TopicsCb,
    TrainCb,
)

QUESTIONS_MIN, QUESTIONS_MAX = 3, 15


def _ids(message: Message) -> tuple[int, int]:
    assert message.from_user is not None
    return message.chat.id, message.from_user.id


def _cb_ids(query: CallbackQuery) -> tuple[int, int, int]:
    assert query.message is not None
    return query.message.chat.id, query.message.message_id, query.from_user.id


def links(text: str, entities: list[MessageEntity] | None) -> list[str]:
    """Ссылки, которые распознал Telegram: `hh.ru/vacancy/1` без схемы и слова-гиперссылки."""
    found = []
    for entity in entities or []:
        if entity.type == "url":
            found.append(entity.extract_from(text))
        elif entity.type == "text_link" and entity.url:
            found.append(entity.url)
    return found


def build_router(trainer: Trainer) -> Router:
    root = Router(name="root")

    @root.message(F.chat.type != ChatType.PRIVATE)
    async def not_private(message: Message) -> None:
        await message.answer(render.PRIVATE_ONLY)

    @root.callback_query(F.message.chat.type != ChatType.PRIVATE)
    async def not_private_button(query: CallbackQuery) -> None:
        await query.answer(render.PRIVATE_ONLY, show_alert=True)

    router = Router(name="sobesnik")
    root.include_router(router)

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await trainer.start(*_ids(message))

    @router.message(Command("help"))
    async def help_(message: Message) -> None:
        await trainer.start(*_ids(message), help=True)

    @router.message(Command("new"))
    async def new(message: Message) -> None:
        await trainer.new_vacancy(*_ids(message))

    async def count_arg(message: Message, command: CommandObject) -> int | None:
        """Число вопросов из `/train N` и `/repeat N`: 0 — не задано, неверное — сообщение
        и `None`."""
        if not command.args:
            return 0
        arg = command.args.strip()
        if not arg.isdigit() or not QUESTIONS_MIN <= int(arg) <= QUESTIONS_MAX:
            await message.answer(
                f"Число вопросов — от {QUESTIONS_MIN} до {QUESTIONS_MAX}: /{command.command} 5"
            )
            return None
        return int(arg)

    @router.message(Command("train"))
    async def train(message: Message, command: CommandObject) -> None:
        n = await count_arg(message, command)
        if n is not None:
            await trainer.train(*_ids(message), n or None)

    @router.message(Command("repeat"))
    async def repeat(message: Message, command: CommandObject) -> None:
        n = await count_arg(message, command)
        if n is not None:
            await trainer.repeat(*_ids(message), n or None)

    @router.message(Command("progress"))
    async def progress(message: Message) -> None:
        await trainer.progress(*_ids(message))

    @router.message(Command("forget"))
    async def forget(message: Message) -> None:
        await trainer.forget(*_ids(message))

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message) -> None:
        await message.answer("Не знаю такой команды. /help — список.")

    @router.message(F.text)
    async def text(message: Message) -> None:
        assert message.text is not None
        found = links(message.text, message.entities)
        await trainer.incoming_text(*_ids(message), message.text, found)

    @router.message(F.voice)
    async def voice(message: Message) -> None:
        assert message.voice is not None
        await trainer.voice(
            *_ids(message),
            file_id=message.voice.file_id,
            size=message.voice.file_size,
            filename="voice.ogg",
            mime=message.voice.mime_type or "audio/ogg",
        )

    @router.message(F.video_note)
    async def video_note(message: Message) -> None:
        """Кружочек: API оставит только звуковую дорожку."""
        assert message.video_note is not None
        await trainer.voice(
            *_ids(message),
            file_id=message.video_note.file_id,
            size=message.video_note.file_size,
            filename="circle.mp4",
            mime="video/mp4",
        )

    @router.message(F.audio)
    async def audio(message: Message) -> None:
        assert message.audio is not None
        await trainer.voice(
            *_ids(message),
            file_id=message.audio.file_id,
            size=message.audio.file_size,
            filename=message.audio.file_name or "audio",
            mime=message.audio.mime_type or "application/octet-stream",
        )

    @router.message(F.document.file_name.lower().endswith((".html", ".htm")))
    async def html(message: Message) -> None:
        assert message.document is not None
        await trainer.html(*_ids(message), message.document.file_id, message.document.file_size)

    @router.message(F.document.mime_type.startswith("audio/"))
    async def audio_file(message: Message) -> None:
        assert message.document is not None
        doc = message.document
        await trainer.voice(
            *_ids(message),
            file_id=doc.file_id,
            size=doc.file_size,
            filename=doc.file_name or "audio",
            mime=doc.mime_type or "application/octet-stream",
        )

    @router.message(F.caption)
    async def caption(message: Message) -> None:
        """Вакансия пересылкой из канала: фото или файл с подписью."""
        assert message.caption is not None
        found = links(message.caption, message.caption_entities)
        await trainer.incoming_text(*_ids(message), message.caption, found)

    @router.message()
    async def other(message: Message) -> None:
        await message.answer("Пришлите текст, ссылку или голосовое. /help — что я умею.")

    @router.callback_query(LevelCb.filter())
    async def level(query: CallbackQuery, callback_data: LevelCb) -> None:
        chat_id, message_id, _ = _cb_ids(query)
        error = await trainer.set_level(
            chat_id, message_id, callback_data.vacancy_id, callback_data.level
        )
        await query.answer(error or "Уровень изменён", show_alert=error is not None)

    @router.callback_query(TrainCb.filter())
    async def train_button(query: CallbackQuery, callback_data: TrainCb) -> None:
        await query.answer()
        chat_id, _, telegram_id = _cb_ids(query)
        await trainer.train(
            chat_id,
            telegram_id,
            callback_data.n or None,
            vacancy_id=callback_data.vacancy_id,
            new=callback_data.new,
        )

    @router.callback_query(SkipCb.filter())
    async def skip(query: CallbackQuery, callback_data: SkipCb) -> None:
        chat_id, _, telegram_id = _cb_ids(query)
        await query.answer(await trainer.skip(chat_id, telegram_id, callback_data.question_id))

    @router.callback_query(DetailsCb.filter())
    async def details(query: CallbackQuery, callback_data: DetailsCb) -> None:
        await query.answer()
        chat_id, _, _ = _cb_ids(query)
        await trainer.details(chat_id, callback_data.answer_id)

    @router.callback_query(NewVacancyCb.filter())
    async def new_vacancy(query: CallbackQuery) -> None:
        await query.answer()
        chat_id, _, telegram_id = _cb_ids(query)
        await trainer.new_vacancy(chat_id, telegram_id)

    @router.callback_query(ForgetCb.filter())
    async def forget_action(query: CallbackQuery, callback_data: ForgetCb) -> None:
        await query.answer()
        await trainer.forget_action(*_cb_ids(query), callback_data.action)

    @router.callback_query(ProgressCb.filter())
    async def progress_button(query: CallbackQuery) -> None:
        await query.answer()
        chat_id, _, telegram_id = _cb_ids(query)
        await trainer.progress(chat_id, telegram_id)

    @router.callback_query(RepeatCb.filter())
    async def repeat_button(query: CallbackQuery) -> None:
        await query.answer()
        chat_id, _, telegram_id = _cb_ids(query)
        await trainer.repeat(chat_id, telegram_id)

    @router.callback_query(TopicsCb.filter())
    async def topics(query: CallbackQuery, callback_data: TopicsCb) -> None:
        chat_id, message_id, telegram_id = _cb_ids(query)
        error: str | None = None
        if callback_data.action == "open" and callback_data.vacancy_id is not None:
            error = await trainer.pick_open(chat_id, telegram_id, callback_data.vacancy_id)
        elif callback_data.action == "toggle":
            error = await trainer.pick_toggle(chat_id, message_id, telegram_id, callback_data.i)
        elif callback_data.action == "start":
            # ответить на нажатие сразу: генерация идёт до минуты
            if await trainer.has_picked(telegram_id):
                await query.answer()
                await trainer.pick_start(chat_id, message_id, telegram_id)
                return
            error = render.PICK_NONE
        else:
            await trainer.pick_cancel(chat_id, message_id, telegram_id)
        await query.answer(error or None, show_alert=error is not None)

    return root
