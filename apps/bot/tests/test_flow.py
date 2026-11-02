"""Сценарии С1–С3 в боте (006 §4, §11): апдейты идут через диспетчер, API и Telegram — фейки."""

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendChatAction, SendMessage

from sobesnik_bot import render
from sobesnik_bot.__main__ import build_dispatcher
from sobesnik_bot.api import ApiError, ApiUnavailable
from sobesnik_bot.flow import Trainer, vacancy_source
from sobesnik_bot.render import DetailsCb, ForgetCb, LevelCb, SkipCb, TrainCb
from sobesnik_bot.state import StateStore

from .conftest import USER, Chat_, Clock, FakeApi, FakeSession


async def with_vacancy(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("Ищем Python-разработчика: asyncio, PostgreSQL")
    tg.clear()


async def in_training(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await with_vacancy(user, api, tg)
    await user.send("/train")
    tg.clear()


def test_vacancy_source() -> None:
    assert vacancy_source(" https://hh.ru/vacancy/1 ") == {"url": "https://hh.ru/vacancy/1"}
    assert vacancy_source("Ищем https://x.ru разработчика") == {
        "text": "Ищем https://x.ru разработчика"
    }


async def test_start_creates_user(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("/start")
    assert api.calls == [("upsert_user", {"telegram_id": USER})]
    assert tg.last_text() == render.INTRO
    await user.send("/help")
    assert api.names() == ["upsert_user"]  # пользователь уже известен
    assert "/forget" in tg.last_text()


async def test_vacancy_text_and_level(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("Ищем Python-разработчика: asyncio, PostgreSQL")
    assert api.names() == ["upsert_user", "create_vacancy"]
    assert api.calls[1][1]["text"].startswith("Ищем")
    sent = tg.of(SendMessage)[0]
    assert sent.text == render.PARSING
    edit = tg.of(EditMessageText)[-1]
    assert "<b>Python-разработчик (Middle)</b>" in edit.text
    assert "✱ Asyncio" in edit.text
    assert edit.reply_markup is not None

    await user.press(LevelCb(vacancy_id=api.vacancy.id, level="senior").pack())
    assert api.calls[-1] == ("set_level", {"vacancy_id": api.vacancy.id, "level": "senior"})
    assert "(Senior)" in tg.of(EditMessageText)[-1].text
    assert tg.of(AnswerCallbackQuery)[-1].text == "Уровень изменён"


async def test_vacancy_url_and_html(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("https://hh.ru/vacancy/123")
    assert api.calls[-1][1]["url"] == "https://hh.ru/vacancy/123"
    tg.files["doc-1"] = "<html>Вакансия</html>".encode()
    await user.document("vacancy.HTML")
    assert api.calls[-1][1]["html"] == "<html>Вакансия</html>"


async def test_vacancy_errors(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    api.errors["create_vacancy"] = ApiError(
        422, "not_a_vacancy", "Похоже на резюме. Пришлите <текст>."
    )
    await user.send("Я Python-разработчик, ищу работу")
    assert tg.last_text() == "Похоже на резюме. Пришлите &lt;текст&gt;."
    api.errors["create_vacancy"] = ApiError(503, "llm_unavailable", "ollama down")
    await user.send("вакансия")
    assert tg.last_text() == render.API_ERRORS["llm_unavailable"]
    api.errors["upsert_user"] = ApiUnavailable("ConnectError")
    await Chat_(user.dp, user.bot, user_id=2002).send("/start")  # новый пользователь
    assert tg.last_text() == render.API_ERRORS["api_unavailable"]


async def test_train_requires_vacancy(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("/train")
    assert tg.last_text() == render.NO_VACANCY
    await user.send("/train 99")
    assert "от 3 до 15" in tg.last_text()


async def test_text_answer_full_session(
    user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer, store: StateStore
) -> None:
    await with_vacancy(user, api, tg)
    await user.press(TrainCb(vacancy_id=api.vacancy.id).pack())
    assert api.calls[-1] == ("create_session", {"vacancy_id": api.vacancy.id, "n": 3})
    assert tg.texts()[0] == render.preparing(3)
    question = tg.of(SendMessage)[-1]
    assert question.text.startswith("<b>Вопрос 1 из 3</b> · тема: db.topic1")
    assert "&lt;index&gt;" in question.text  # текст вопроса экранирован

    for position in (1, 2, 3):
        tg.clear()
        await user.send(f"мой ответ {position}")
        answer = api.last_answer()
        assert api.calls[-1][1]["text"] == f"мой ответ {position}"
        status = tg.sent[0]
        assert status.text == "✅ Принято. 🧠 Оцениваю ответ…"

        await user.send("ещё текст")  # пока ждём оценку — не ответ и не вакансия
        assert tg.last_text() == render.WAIT_EVALUATION

        api.set_status(answer.id, "evaluating")
        await trainer.on_event(answer.id, "evaluating")
        assert not tg.of(EditMessageText)  # текст: статус уже «Оцениваю»

        api.evaluate(answer.id, total=67 + position)
        await trainer.on_event(answer.id, "done")
        result = tg.of(EditMessageText)[-1]
        assert result.message_id == status.message_id
        assert "≈ 70 из 100" in result.text
        assert ("Балл примерный" in result.text) == (position == 1)  # оговорка — один раз
        await trainer.on_event(answer.id, "done")  # повтор события ничего не меняет
        assert len(tg.of(EditMessageText)) == 1

    assert "finish_session" in api.names()
    summary = tg.of(SendMessage)[-1]
    assert summary.text.startswith("🏁 <b>Сессия завершена</b>")
    assert "Отвечено 3 из 3" in summary.text
    state = await store.get_user(USER)
    assert (state.mode, state.session_id) == ("idle", None)
    assert await store.all_pending() == []


async def test_skip_and_summary(
    user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer
) -> None:
    await in_training(user, api, tg)
    assert api.session is not None
    first, second, _ = api.session.questions
    await user.press(SkipCb(question_id=second.id).pack())  # не текущий вопрос
    assert tg.of(AnswerCallbackQuery)[-1].text == "Этот вопрос уже не текущий."
    for q in (first, second):
        await user.press(SkipCb(question_id=q.id).pack())
    assert tg.last_text().startswith("<b>Вопрос 3 из 3</b>")
    await user.send("ответ")
    answer = api.last_answer()
    api.evaluate(answer.id, total=30, passed=False)
    await trainer.on_event(answer.id, "done")
    summary = tg.last_text()
    assert "Отвечено 1 из 3 · зачтено 0" in summary
    assert "1. — db.topic1: пропущен" in summary
    assert "3. 🔴 db.topic3 — ≈ 30" in summary


async def test_voice_answer(user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer) -> None:
    await in_training(user, api, tg)
    tg.files["voice-1"] = b"OggS-voice"
    await user.voice()
    assert api.voice_data == [(b"OggS-voice", "voice.ogg", "audio/ogg")]
    answer = api.last_answer()
    status = tg.of(SendMessage)[-1]
    assert status.text == "✅ Принято. 🎧 Распознаю речь…"

    api.set_status(answer.id, "transcribing")
    await trainer.on_event(answer.id, "transcribing")
    assert not tg.of(EditMessageText)

    api.set_status(answer.id, "evaluating", transcript="ивент луп и <корутины>")
    await trainer.on_event(answer.id, "evaluating")
    edit = tg.of(EditMessageText)[-1]
    assert edit.text.startswith("✅ Принято. 🧠 Оцениваю ответ…")
    assert "<blockquote expandable>ивент луп и &lt;корутины&gt;</blockquote>" in edit.text

    api.evaluate(answer.id, doubts=["«ивент луп» — возможно, «event loop»", "2", "3", "4"])
    await trainer.on_event(answer.id, "done")
    result = tg.of(EditMessageText)[-1].text
    assert "Возможно, распознано неверно — проверьте" in result
    assert "• 3" in result and "• 4" not in result  # не больше трёх
    assert tg.last_text().startswith("<b>Вопрос 2 из 3</b>")


async def test_voice_failures(user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer) -> None:
    await user.voice()
    assert tg.last_text() == render.VOICE_NOT_NOW
    await in_training(user, api, tg)

    await user.voice(size=25 * 1024 * 1024)
    assert tg.last_text() == render.FILE_TOO_BIG

    tg.files["voice-1"] = b"x"
    api.errors["answer_voice"] = ApiError(422, "audio_too_long", "длиннее 300 с")
    await user.voice()
    assert tg.last_text() == render.API_ERRORS["audio_too_long"]

    await user.voice()
    answer = api.last_answer()
    api.set_status(answer.id, "failed", error="asr_empty")
    await trainer.on_event(answer.id, "failed")
    failed = tg.of(EditMessageText)[-1]
    assert failed.text.startswith(render.FAILURES["asr_empty"])
    assert failed.reply_markup is not None  # можно пропустить
    # тот же вопрос снова ждёт ответа
    await user.send("отвечу текстом")
    assert api.calls[-1][0] == "answer_text"
    assert api.calls[-1][1]["question_id"] == answer.question_id


async def test_slow_note_and_reconcile(
    user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer, clock: Clock
) -> None:
    await in_training(user, api, tg)
    await user.send("ответ")
    answer = api.last_answer()
    api.set_status(answer.id, "evaluating")
    await trainer.reconcile()  # меньше минуты — не трогаем
    assert not tg.of(EditMessageText)

    clock.t += 61
    await trainer.reconcile()
    assert "думает дольше обычного" in tg.of(EditMessageText)[-1].text

    api.evaluate(answer.id)  # событие потерялось — сверка находит результат сама
    clock.t += 30
    await trainer.reconcile()
    assert "≈ 65 из 100" in tg.of(EditMessageText)[-1].text
    assert tg.last_text().startswith("<b>Вопрос 2 из 3</b>")


async def test_restart_resumes_pending(
    user: Chat_,
    api: FakeApi,
    tg: FakeSession,
    trainer: Trainer,
    bot: Bot,
    store: StateStore,
    clock: Clock,
) -> None:
    """Бот перезапустился, пока ответ оценивался: новый `Trainer` подхватывает результат."""
    await in_training(user, api, tg)
    await user.send("ответ")
    answer = api.last_answer()
    await trainer.close()

    api.evaluate(answer.id)
    restarted = Trainer(bot, api, store, questions=3, now=clock)
    try:
        await restarted.resume()
        assert "≈ 65 из 100" in tg.of(EditMessageText)[-1].text
        assert tg.last_text().startswith("<b>Вопрос 2 из 3</b>")
        assert tg.of(SendChatAction)
    finally:
        await restarted.close()


async def test_resume_session_with_train(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await in_training(user, api, tg)
    await user.send("/train")
    assert api.names().count("create_session") == 1
    assert tg.last_text().startswith("<b>Вопрос 1 из 3</b>")
    await user.send("/train 4")  # с числом — новая сессия
    assert api.names().count("create_session") == 2


async def test_api_errors_on_answer(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await in_training(user, api, tg)
    api.errors["answer_text"] = ApiError(409, "already_answered", "уже есть")
    await user.send("ответ")
    assert tg.last_text() == render.API_ERRORS["already_answered"]
    api.errors["answer_text"] = ApiError(500, "boom", "x")
    await user.send("ответ")
    assert tg.last_text() == "Что-то пошло не так (код: boom)."


async def test_details_button(user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer) -> None:
    await in_training(user, api, tg)
    await user.send("ответ")
    answer = api.last_answer()
    api.evaluate(answer.id)
    await trainer.on_event(answer.id, "done")
    await user.press(DetailsCb(answer_id=answer.id).pack())
    assert tg.last_text().startswith("<b>Почему такие баллы</b>")
    assert "почему depth" in tg.last_text()


async def test_forget(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("/forget")
    assert tg.last_text() == render.FORGET_CONFIRM
    await user.press(ForgetCb(confirm=False).pack())
    assert tg.last_text() == render.FORGET_CANCELLED
    assert "forget_audio" not in api.names()
    await user.press(ForgetCb(confirm=True).pack())
    assert api.calls[-1] == ("forget_audio", {"user_id": api.user_id})
    assert tg.last_text() == "Удалено записей: 2, расшифровок: 2."
    api.errors["forget_audio"] = ApiError(503, "storage_unavailable", "s3")
    await user.press(ForgetCb(confirm=True).pack())
    assert tg.last_text() == render.API_ERRORS["storage_unavailable"]


async def test_allowlist(trainer: Trainer, bot: Bot, api: FakeApi, tg: FakeSession) -> None:
    dp = build_dispatcher(trainer, frozenset({USER}))
    stranger = Chat_(dp, bot, user_id=666)
    await stranger.send("/start")
    await stranger.send("вакансия")
    await stranger.press(ForgetCb(confirm=True).pack())
    assert api.calls == []
    assert tg.texts() == [render.NOT_ALLOWED, render.NOT_ALLOWED]
    assert tg.of(AnswerCallbackQuery)[-1].text == render.NOT_ALLOWED

    await Chat_(dp, bot).send("/start")
    assert api.names() == ["upsert_user"]
