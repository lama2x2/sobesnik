"""Обработчики Telegram (006 §4): разбирают апдейт и передают его в `Trainer`."""

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from sobesnik_bot.flow import Trainer
from sobesnik_bot.render import DetailsCb, ForgetCb, LevelCb, NewVacancyCb, SkipCb, TrainCb

QUESTIONS_MIN, QUESTIONS_MAX = 3, 15


def _ids(message: Message) -> tuple[int, int]:
    assert message.from_user is not None
    return message.chat.id, message.from_user.id


def _cb_ids(query: CallbackQuery) -> tuple[int, int, int]:
    assert query.message is not None
    return query.message.chat.id, query.message.message_id, query.from_user.id


def build_router(trainer: Trainer) -> Router:
    router = Router(name="sobesnik")

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await trainer.start(*_ids(message))

    @router.message(Command("help"))
    async def help_(message: Message) -> None:
        await trainer.start(*_ids(message), help=True)

    @router.message(Command("new"))
    async def new(message: Message) -> None:
        await trainer.new_vacancy(*_ids(message))

    @router.message(Command("train"))
    async def train(message: Message, command: CommandObject) -> None:
        n = None
        if command.args:
            arg = command.args.strip()
            if not arg.isdigit() or not QUESTIONS_MIN <= int(arg) <= QUESTIONS_MAX:
                await message.answer(
                    f"Число вопросов — от {QUESTIONS_MIN} до {QUESTIONS_MAX}: /train 5"
                )
                return
            n = int(arg)
        await trainer.train(*_ids(message), n)

    @router.message(Command("forget"))
    async def forget(message: Message) -> None:
        await trainer.forget(*_ids(message))

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message) -> None:
        await message.answer("Не знаю такой команды. /help — список.")

    @router.message(F.text)
    async def text(message: Message) -> None:
        assert message.text is not None
        await trainer.text(*_ids(message), message.text)

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
            chat_id, telegram_id, vacancy_id=callback_data.vacancy_id, new=callback_data.new
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
    async def forget_confirmed(query: CallbackQuery, callback_data: ForgetCb) -> None:
        await query.answer()
        await trainer.forget_confirmed(*_cb_ids(query), callback_data.confirm)

    return router
