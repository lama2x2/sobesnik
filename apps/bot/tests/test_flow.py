"""Сценарии С1–С3 в боте (006 §4, §11): апдейты идут через диспетчер, API и Telegram — фейки."""

import asyncio
import contextlib
import logging
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendChatAction, SendMessage

from sobesnik_bot import render
from sobesnik_bot.__main__ import build_dispatcher
from sobesnik_bot.api import (
    ApiError,
    ApiUnavailable,
    Doc,
    LlmStatus,
    Review,
    ReviewMistake,
    ReviewPoint,
)
from sobesnik_bot.flow import Trainer, vacancy_source
from sobesnik_bot.render import DetailsCb, ForgetCb, LevelCb, ReviewCb, SkipCb, TrainCb
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
    hh = "https://hh.ru/vacancy/1"
    assert vacancy_source(f" {hh} ") == {"url": hh}
    assert vacancy_source(f"Вот вакансия: {hh}, посмотри") == {"url": hh}
    assert vacancy_source("hh.ru/vacancy/1", ["hh.ru/vacancy/1"]) == {"url": hh}
    assert vacancy_source("вот эта", [hh]) == {"url": hh}  # слово-гиперссылка
    assert vacancy_source("HTTP://X.RU/1") == {"url": "HTTP://X.RU/1"}
    long = "Ищем Python-разработчика, требования: asyncio, PostgreSQL, Docker. " * 3
    assert vacancy_source(f"{long} {hh}") == {"text": f"{long} {hh}".strip()}
    two = f"{hh} https://hh.ru/vacancy/2"
    assert vacancy_source(two) == {"text": two}


async def test_start_creates_user(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("/start")
    assert api.calls == [("upsert_user", {"telegram_id": USER})]
    assert tg.last_text() == render.INTRO
    await user.send("/help")
    assert api.names() == ["upsert_user"]  # пользователь уже известен
    assert "/forget" in tg.last_text()


async def test_start_warns_when_model_missing(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    """Модель не готова — предупреждение сразу на /start, до вакансии (008 §7)."""
    api.llm = LlmStatus(ok=False, model="qwen3.5:4b", hint="Выполните: ollama pull qwen3.5:4b")
    await user.send("/start")
    assert tg.last_text().startswith(render.INTRO)
    assert tg.last_text().endswith("⚠️ Выполните: ollama pull qwen3.5:4b")


async def test_start_without_llm_status(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    api.errors["llm_status"] = ApiUnavailable("ConnectError")
    await user.send("/start")
    assert tg.last_text() == render.INTRO


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
    assert question.text.startswith("<b>Вопрос 1 из 3</b> · тема: Индексы\n")
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
    assert "1. — Индексы: пропущен" in summary
    assert "3. 🔴 Индексы — ≈ 30" in summary


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


async def test_first_voice_warns_model_is_downloading(
    user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer
) -> None:
    """Whisper ещё качается — статус честно говорит, что это надолго (011 §4)."""
    await in_training(user, api, tg)
    api.asr_ready = False
    tg.files["voice-1"] = b"OggS-voice"
    await user.voice()
    answer = api.last_answer()
    assert tg.of(SendMessage)[-1].text == f"✅ Принято. {render.ASR_FIRST_TIME}"

    api.set_status(answer.id, "transcribing", asr_ready=False)
    await trainer.on_event(answer.id, "transcribing")
    assert not tg.of(EditMessageText)  # текст тот же — не редактируем

    api.set_status(answer.id, "transcribing", asr_ready=True)  # модель докачалась
    await trainer.sync(answer.id)  # событий нет — сверка по таймеру
    assert tg.of(EditMessageText)[-1].text == "✅ Принято. 🎧 Распознаю речь…"


async def test_video_note_answer(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    """Кружочек уходит в API как mp4 — звук отделит API."""
    await in_training(user, api, tg)
    tg.files["circle-1"] = b"mp4-circle"
    await user.video_note()
    assert api.voice_data == [(b"mp4-circle", "circle.mp4", "video/mp4")]
    assert tg.of(SendMessage)[-1].text == "✅ Принято. 🎧 Распознаю речь…"


async def test_question_count_buttons(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await with_vacancy(user, api, tg)
    keyboard = render.vacancy_keyboard(api.vacancy).inline_keyboard
    assert [b.text for b in keyboard[-2]] == ["10 вопросов", "15 вопросов"]
    await user.press(TrainCb(vacancy_id=api.vacancy.id, n=10).pack())
    assert api.calls[-1] == ("create_session", {"vacancy_id": api.vacancy.id, "n": 10})
    assert tg.last_text().startswith("<b>Вопрос 1 из 10</b>")


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


async def test_api_error_hint_is_shown(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    """У ошибки модели есть подсказка API — показываем её, а не общий текст (008 §7)."""
    api.errors["create_vacancy"] = ApiError(
        503, "llm_model_missing", "не найдена", "Модель x не скачана: ollama pull x"
    )
    await user.send("Ищем Python-разработчика: asyncio, PostgreSQL")
    assert tg.last_text() == "Модель x не скачана: ollama pull x"
    assert "ollama pull" in render.API_ERRORS["llm_model_missing"]
    assert "ollama pull" in render.FAILURES["llm_model_missing"]


async def test_ack_ms_logged(
    user: Chat_, api: FakeApi, tg: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Время подтверждения приёма — в логе, для замера 000 §5."""
    await in_training(user, api, tg)
    with caplog.at_level(logging.INFO, logger="sobesnik_bot"):
        await user.send("ответ")
    accepted = [r for r in caplog.records if r.getMessage() == "answer accepted"]
    assert accepted[0].ack_ms >= 0  # type: ignore[attr-defined]


async def test_details_button(user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer) -> None:
    await in_training(user, api, tg)
    await user.send("ответ")
    answer = api.last_answer()
    api.evaluate(answer.id)
    await trainer.on_event(answer.id, "done")
    await user.press(DetailsCb(answer_id=answer.id).pack())
    assert tg.last_text().startswith("<b>Почему такие баллы</b>")
    assert "почему depth" in tg.last_text()


async def test_review_button(user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer) -> None:
    await in_training(user, api, tg)
    await user.send("ответ")
    answer = api.last_answer()
    api.evaluate(answer.id)
    await trainer.on_event(answer.id, "done")
    evaluation = tg.of(EditMessageText)[-1]
    assert ReviewCb(answer_id=answer.id).pack() in str(evaluation.reply_markup)

    await user.press(ReviewCb(answer_id=answer.id).pack())
    assert api.calls[-1] == ("review_answer", {"answer_id": answer.id})
    text = tg.last_text()
    assert text.startswith("<b>📖 Разбор</b>")
    assert "✅ упорядочен по a — верно" in text
    assert "❌ нужен индекс по b — почему" in text
    assert "«индекс всегда &lt;быстрее&gt;» → не всегда" in text
    assert "В самом вопросе" not in text
    assert text.endswith(
        '<a href="https://www.postgresql.org/docs/current/indexes.html">Индексы</a>'
    )

    api.errors["review_answer"] = ApiError(409, "transcript_deleted", "нет")
    await user.press(ReviewCb(answer_id=answer.id).pack())
    assert tg.last_text() == render.REVIEW_ERRORS["transcript_deleted"]
    api.errors["review_answer"] = ApiError(503, "llm_unavailable", "нет")
    await user.press(ReviewCb(answer_id=answer.id).pack())
    assert tg.last_text() == render.API_ERRORS["llm_unavailable"]


def test_review_text_fits_telegram() -> None:
    review = Review(
        reference="эталон " * 300,
        points=[
            ReviewPoint(n=n, text="пункт", status="covered", comment="к" * 400) for n in range(1, 7)
        ],
        mistakes=[ReviewMistake(quote="ц" * 400, correction="и" * 400) for _ in range(3)],
        question_issue="вопрос неверен",
        docs=[Doc(title="Док", url="https://example.org")],
    )
    text = render.review_text(review)
    assert len(text) <= render.REVIEW_MAX_CHARS
    assert text.endswith('<a href="https://example.org">Док</a>')
    assert "Как мог бы ответить" in text


def test_review_shows_key_point_doubt() -> None:
    """Сомнение фоновой проверки пунктов — под пунктом (011 §6)."""
    review = Review(
        reference="эталон",
        points=[
            ReviewPoint(n=1, text="пункт <1>", status="missed", comment="", doubt="наоборот <b>")
        ],
        mistakes=[],
        question_issue=None,
        docs=[],
    )
    text = render.review_text(review)
    assert "⚠️ <i>Этот пункт эталона, возможно, неверен: наоборот &lt;b&gt;</i>" in text


async def test_forget(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("/forget")
    assert tg.last_text() == render.FORGET_ASK
    await user.press(ForgetCb(action="cancel").pack())
    assert tg.last_text() == render.FORGET_CANCELLED
    assert "forget_audio" not in api.names()
    await user.press(ForgetCb(action="audio").pack())
    assert api.calls[-1] == ("forget_audio", {"user_id": api.user_id})
    assert tg.last_text() == "Удалено записей: 2, расшифровок: 2."
    api.errors["forget_audio"] = ApiError(503, "storage_unavailable", "s3")
    await user.press(ForgetCb(action="audio").pack())
    assert tg.last_text() == render.API_ERRORS["storage_unavailable"]


async def test_allowlist(trainer: Trainer, bot: Bot, api: FakeApi, tg: FakeSession) -> None:
    dp = build_dispatcher(trainer, frozenset({USER}))
    stranger = Chat_(dp, bot, user_id=666)
    await stranger.send("/start")
    await stranger.send("вакансия")
    await stranger.press(ForgetCb(action="all_confirm").pack())
    assert api.calls == []
    denied = render.not_allowed(666)
    assert "666" in denied and "BOT_ALLOWED_IDS" in denied
    assert tg.texts() == [denied, denied]
    assert tg.of(AnswerCallbackQuery)[-1].text == denied

    await Chat_(dp, bot).send("/start")
    assert api.names() == ["upsert_user"]


# --- пограничные случаи ввода (006 §4.6)

PART = "Требования: Python, asyncio, PostgreSQL. " * 100  # ~4 000 символов, как часть от Telegram


async def test_split_vacancy_is_glued(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send(PART)
    assert "create_vacancy" not in api.names()  # ждёт продолжения
    await user.send("Ключевые навыки: Docker, Kafka")
    texts = [kw["text"] for name, kw in api.calls if name == "create_vacancy"]
    assert texts == [PART + "\nКлючевые навыки: Docker, Kafka"]


async def test_split_vacancy_glued_by_timeout(
    user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer
) -> None:
    await user.send(PART)
    await user.send(PART)
    assert "create_vacancy" not in api.names()
    await trainer.wait_glued()
    texts = [kw["text"] for name, kw in api.calls if name == "create_vacancy"]
    assert texts == [(PART + "\n" + PART).strip()]


async def test_split_answer_is_glued(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await in_training(user, api, tg)
    await user.send(PART)
    await user.send("конец ответа")
    answers = [kw["text"] for name, kw in api.calls if name == "answer_text"]
    assert answers == [PART + "\nконец ответа"]


async def test_busy_while_parsing(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    api.gate = asyncio.Event()
    first = asyncio.create_task(user.send("Ищем Python-разработчика: asyncio, PostgreSQL"))
    for _ in range(20):
        await asyncio.sleep(0)
    await user.send("Ещё одна вакансия: Go, Kubernetes")
    await user.send("/train")
    assert tg.texts().count(render.BUSY) == 2
    api.gate.set()
    await first
    assert api.names().count("create_vacancy") == 1
    assert "create_session" not in api.names()


async def test_link_from_entities(user: Chat_, api: FakeApi) -> None:
    text = "глянь hh.ru/vacancy/42"
    await user.send(text, entities=[{"type": "url", "offset": 6, "length": 16}])
    assert api.calls[-1][1]["url"] == "https://hh.ru/vacancy/42"
    await user.send(
        "вот эта вакансия",
        entities=[{"type": "text_link", "offset": 4, "length": 3, "url": "https://hh.ru/v/7"}],
    )
    assert api.calls[-1][1]["url"] == "https://hh.ru/v/7"


async def test_caption_is_vacancy(user: Chat_, api: FakeApi) -> None:
    await user.photo("Ищем Python-разработчика в команду платформы: asyncio, PostgreSQL, Docker")
    assert api.calls[-1][1]["text"].startswith("Ищем Python-разработчика")


async def test_audio_document_is_voice(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await in_training(user, api, tg)
    tg.files["doc-1"] = b"ID3-mp3"
    await user.document("answer.mp3", mime="audio/mpeg")
    assert api.voice_data == [(b"ID3-mp3", "answer.mp3", "audio/mpeg")]


async def test_too_long_messages(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    api.errors["create_vacancy"] = ApiError(422, "validation_error", "x")
    await user.send("очень длинная вакансия")
    assert tg.last_text() == render.VACANCY_TOO_LONG
    await in_training(user, api, tg)
    api.errors["answer_text"] = ApiError(422, "validation_error", "x")
    await user.send("очень длинный ответ")
    assert tg.last_text() == render.ANSWER_TOO_LONG


async def test_group_chat_ignored(
    trainer: Trainer, bot: Bot, api: FakeApi, tg: FakeSession
) -> None:
    group = Chat_(build_dispatcher(trainer, frozenset()), bot, user_id=-100, chat_type="group")
    await group.send("/start")
    await group.send("вакансия")
    assert api.calls == []
    assert tg.texts() == [render.PRIVATE_ONLY, render.PRIVATE_ONLY]


# --- Telegram недоступен (живая проверка: VPN пропал на 5 минут)


async def test_send_retries_network_errors(user: Chat_, tg: FakeSession) -> None:
    tg.fail_next = 2
    await user.send("/start")
    assert tg.last_text() == render.INTRO


async def test_telegram_down_after_generation_keeps_session(
    user: Chat_, api: FakeApi, tg: FakeSession
) -> None:
    """Вопросы сгенерированы, а отправить их некуда: /train потом продолжает ту же сессию."""
    await with_vacancy(user, api, tg)
    sent = 0  # «готовлю» уходит, дальше Telegram пропадает надолго
    original = tg.make_request

    async def flaky(bot: Any, method: Any, timeout: int | None = None) -> Any:  # noqa: ASYNC109
        nonlocal sent
        sent += 1
        if sent > 1:
            raise TelegramNetworkError(method=method, message="timeout")
        return await original(bot, method, timeout)

    tg.make_request = flaky  # type: ignore[method-assign]
    with contextlib.suppress(TelegramNetworkError):
        await user.send("/train")
    tg.make_request = original  # type: ignore[method-assign]

    await user.send("/train")
    assert api.names().count("create_session") == 1
    assert tg.last_text().startswith("<b>Вопрос 1 из 3</b>")


async def test_lost_result_message_does_not_block(
    user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer
) -> None:
    await in_training(user, api, tg)
    await user.send("ответ")
    answer = api.last_answer()
    api.evaluate(answer.id)
    tg.fail_next = 3  # правка статуса не прошла ни с одной попытки
    await trainer.on_event(answer.id, "done")
    assert tg.last_text().startswith("<b>Вопрос 2 из 3</b>")
