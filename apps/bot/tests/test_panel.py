"""Постоянная панель и порядок разбора (012): API и Telegram — фейки."""

import asyncio

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import ReplyKeyboardMarkup

from sobesnik_bot import render
from sobesnik_bot.__main__ import COMMANDS
from sobesnik_bot.api import Progress
from sobesnik_bot.flow import SPLIT_FROM, Trainer
from sobesnik_bot.state import StateStore

from .conftest import USER, Chat_, FakeApi, FakeSession

BUTTONS = [
    render.BUTTON_NEW,
    render.BUTTON_TRAIN,
    render.BUTTON_PROGRESS,
    render.BUTTON_REPEAT,
]
OTHER_USER = 2002


async def in_training(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("Ищем Python-разработчика: asyncio, PostgreSQL")
    await user.send("/train")
    tg.clear()
    api.calls.clear()


async def answered(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await in_training(user, api, tg)
    await user.send("Мой ответ")
    tg.clear()
    api.calls.clear()


def panels(tg: FakeSession) -> list[SendMessage]:
    return [m for m in tg.of(SendMessage) if isinstance(m.reply_markup, ReplyKeyboardMarkup)]


def not_answered(api: FakeApi) -> None:
    assert "answer_text" not in api.names()
    assert "create_vacancy" not in api.names()


def test_panel_shape() -> None:
    panel = render.panel()
    rows = [[b.text for b in row] for row in panel.keyboard]
    assert rows == [BUTTONS[:2], BUTTONS[2:]]
    assert panel.resize_keyboard and panel.is_persistent and not panel.one_time_keyboard
    assert {c.command for c in COMMANDS} >= {"web", "forget", "help"}
    assert not any(c in b for b in BUTTONS for c in ("/web", "/forget", "/help"))


def test_panel_command_is_exact() -> None:
    assert render.panel_command(render.BUTTON_PROGRESS) == "progress"
    assert render.panel_command(" ▶ Тренировка ") == "train"  # клиент потерял U+FE0F
    for text in ("Прогресс", "📊 Прогресс моего ответа", "", None):
        assert render.panel_command(text) is None


@pytest.mark.parametrize("command", ["/start", "/help"])
async def test_panel_on_start_and_help(
    dp: Dispatcher, bot: Bot, tg: FakeSession, command: str
) -> None:
    new_user = Chat_(dp, bot, user_id=OTHER_USER)
    await new_user.send(command)
    assert len(tg.of(SendMessage)) == 1  # панель на самом тексте, без подсказки
    assert panels(tg)[0].text.startswith(render.INTRO[:20])
    tg.clear()
    await new_user.send(render.BUTTON_NEW)
    assert tg.texts() == [render.ASK_VACANCY]


async def test_panel_reaches_existing_user_once(
    dp: Dispatcher, bot: Bot, tg: FakeSession, api: FakeApi
) -> None:
    """Кто /start не нажмёт, получает панель перед первым сообщением бота, отдельно от статусов:
    их бот редактирует, а сообщение с reply-клавиатурой Telegram редактировать не даёт."""
    old_user = Chat_(dp, bot, user_id=OTHER_USER)
    await old_user.send("Ищем Python-разработчика: asyncio, PostgreSQL")
    assert [m.text for m in tg.of(SendMessage)] == [render.PANEL_HINT, render.PARSING]
    assert len(panels(tg)) == 1
    assert "create_vacancy" in api.names()
    assert tg.last_text().startswith("<b>Python-разработчик")  # статус отредактирован
    tg.clear()
    await old_user.send("/train")
    assert not panels(tg)


async def test_panel_hint_does_not_eat_answer(
    user: Chat_, tg: FakeSession, api: FakeApi, store: StateStore
) -> None:
    await in_training(user, api, tg)
    await store.unmark_panel(USER)  # бота обновили посреди тренировки
    await user.send("Мой ответ")
    assert api.calls[-1][0] == "answer_text"
    assert api.calls[-1][1]["text"] == "Мой ответ"
    assert tg.of(SendMessage)[0].text == render.PANEL_HINT
    assert (await store.get_user(USER)).mode == "waiting"


async def test_panel_hint_retried_after_network_failure(
    dp: Dispatcher, bot: Bot, tg: FakeSession
) -> None:
    old_user = Chat_(dp, bot, user_id=OTHER_USER)
    tg.fail_next = 3  # все попытки подсказки; сообщение за ней уйдёт
    await old_user.send(render.BUTTON_NEW)
    assert [m.text for m in tg.of(SendMessage)] == [render.ASK_VACANCY]
    tg.clear()
    await old_user.send(render.BUTTON_NEW)
    assert [m.text for m in tg.of(SendMessage)] == [render.PANEL_HINT, render.ASK_VACANCY]


async def test_new_in_answer_mode(
    user: Chat_, api: FakeApi, tg: FakeSession, store: StateStore
) -> None:
    await in_training(user, api, tg)
    await user.send(render.BUTTON_NEW)
    assert tg.texts() == [render.ASK_VACANCY]
    assert (await store.get_user(USER)).mode == "idle"
    not_answered(api)


async def test_train_in_answer_mode_repeats_question(
    user: Chat_, api: FakeApi, tg: FakeSession
) -> None:
    await in_training(user, api, tg)
    await user.send(render.BUTTON_TRAIN)
    assert tg.last_text().startswith("<b>Вопрос 1 из 3</b>")
    assert "answer_text" not in api.names()


async def test_progress_in_answer_mode_keeps_question(
    user: Chat_, api: FakeApi, tg: FakeSession, store: StateStore
) -> None:
    await in_training(user, api, tg)
    before = await store.get_user(USER)
    await user.send(render.BUTTON_PROGRESS)
    assert api.calls[-1][0] == "get_progress"
    assert tg.last_text() == render.PROGRESS_EMPTY
    assert await store.get_user(USER) == before
    assert "answer_text" not in api.names()


async def test_repeat_in_answer_mode(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await in_training(user, api, tg)
    api.progress = Progress(topics=[], sessions=[], due_count=1)
    await user.send(render.BUTTON_REPEAT)
    assert "create_repeat" in api.names()
    assert "answer_text" not in api.names()


async def test_buttons_while_waiting_for_evaluation(
    user: Chat_, api: FakeApi, tg: FakeSession, store: StateStore
) -> None:
    await answered(user, api, tg)
    for button in (render.BUTTON_TRAIN, render.BUTTON_REPEAT):
        await user.send(button)
        assert tg.last_text() == render.WAIT_EVALUATION
    await user.send(render.BUTTON_PROGRESS)
    assert tg.last_text() == render.PROGRESS_EMPTY
    assert (await store.get_user(USER)).mode == "waiting"

    await user.send(render.BUTTON_NEW)  # как /new: выход, оценка придёт без следующего вопроса
    assert tg.last_text() == render.ASK_VACANCY
    assert (await store.get_user(USER)).mode == "idle"
    not_answered(api)


@pytest.mark.parametrize(
    ("button", "reply"),
    [
        (render.BUTTON_NEW, render.WAIT_EVALUATION),
        (render.BUTTON_TRAIN, render.WAIT_EVALUATION),
        (render.BUTTON_REPEAT, render.WAIT_EVALUATION),
        (render.BUTTON_PROGRESS, render.PROGRESS_EMPTY),
    ],
)
async def test_buttons_while_voice_downloads(
    user: Chat_,
    api: FakeApi,
    tg: FakeSession,
    store: StateStore,
    trainer: Trainer,
    button: str,
    reply: str,
) -> None:
    await in_training(user, api, tg)
    tg.files["voice-1"] = b"OggS"
    tg.download_gate = asyncio.Event()
    voice = asyncio.create_task(user.voice())
    await asyncio.sleep(0.05)
    assert USER in trainer._receiving

    await user.send(button)
    assert tg.last_text() == reply

    tg.download_gate.set()
    await voice
    assert (await store.get_user(USER)).mode == "waiting"  # голосовой ответ не потерян
    assert api.names().count("answer_voice") == 1
    not_answered(api)


@pytest.mark.parametrize("button", [render.BUTTON_NEW, render.BUTTON_TRAIN, render.BUTTON_REPEAT])
async def test_buttons_while_busy(
    user: Chat_, api: FakeApi, tg: FakeSession, store: StateStore, button: str
) -> None:
    api.gate = asyncio.Event()
    parsing = asyncio.create_task(user.send("Ищем Python-разработчика: asyncio"))
    await asyncio.sleep(0.05)
    tg.clear()
    api.calls.clear()

    await user.send(button)
    assert tg.last_text() == render.BUSY
    not_answered(api)

    api.gate.set()
    await parsing


async def test_buttons_in_idle_mode(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send(render.BUTTON_TRAIN)
    assert tg.last_text() == render.NO_VACANCY
    await user.send(render.BUTTON_PROGRESS)
    assert tg.last_text() == render.PROGRESS_EMPTY
    await user.send(render.BUTTON_REPEAT)
    not_answered(api)


async def test_button_not_glued_to_long_answer(
    user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer
) -> None:
    await in_training(user, api, tg)
    first = "а" * SPLIT_FROM
    await user.send(first)
    await user.send(render.BUTTON_PROGRESS)
    await trainer.wait_glued()
    assert api.calls[-1][0] == "answer_text"
    assert api.calls[-1][1]["text"] == first  # таймаут склейки, кнопка в текст не попала


async def test_word_without_emoji_is_answer(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await in_training(user, api, tg)
    await user.send("Прогресс")
    assert api.calls[-1][0] == "answer_text"


async def test_button_in_group_is_refused(
    dp: Dispatcher, bot: Bot, tg: FakeSession, api: FakeApi
) -> None:
    group = Chat_(dp, bot, user_id=-5, chat_type="group")
    await group.send(render.BUTTON_PROGRESS)
    assert tg.texts() == [render.PRIVATE_ONLY]
    assert not panels(tg)
    assert not api.calls
