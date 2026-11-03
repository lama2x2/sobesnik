"""Прогресс, повторение, выбор тем, ссылки под оценкой и /forget всего в боте (007 §8)."""

import uuid
from datetime import UTC, datetime

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from sobesnik_bot import render
from sobesnik_bot.api import (
    ApiError,
    Doc,
    Progress,
    Requirement,
    SessionHistory,
    TopicProgress,
)
from sobesnik_bot.flow import Trainer
from sobesnik_bot.render import ForgetCb, ProgressCb, RepeatCb, SkipCb, TopicsCb
from sobesnik_bot.state import StateStore

from .conftest import USER, Chat_, FakeApi, FakeSession

DOCS = [
    Doc(title="PostgreSQL: индексы", url="https://postgrespro.ru/docs/postgresql/17/indexes"),
    Doc(title="PostgreSQL: EXPLAIN", url="https://postgrespro.ru/docs/postgresql/17/using-explain"),
    Doc(title="Use The Index, Luke", url="https://use-the-index-luke.com/"),
]


def tp(slug: str, status: str, avg: float, attempts: int, *, low: bool = False) -> TopicProgress:
    return TopicProgress(
        slug=slug,
        title={"db.indexes": "Индексы и планы запросов", "db.sql": "SQL: JOIN, агрегация"}.get(
            slug, slug
        ),
        status=status,
        low=low,
        attempts=attempts,
        last_score=int(avg),
        avg_last3=avg,
        docs=DOCS if slug == "db.indexes" else [],
    )


MIXED = Progress(
    topics=[
        tp("db.indexes", "weak", 46.7, 3),
        tp("python.asyncio", "few", 40, 1, low=True),
        tp("web.http", "few", 75, 1),
        tp("db.sql", "ok", 81, 4),
    ],
    sessions=[
        SessionHistory(
            created_at=datetime(2026, 11, 2, tzinfo=UTC),
            mode="vacancy",
            vacancy_title="Python-разработчик",
            answered=4,
            passed=3,
            avg_total=61.0,
            scores=[70, 45, None, 80, 55],
        ),
        SessionHistory(
            created_at=datetime(2026, 11, 1, tzinfo=UTC),
            mode="repeat",
            vacancy_title=None,
            answered=2,
            passed=2,
            avg_total=80.0,
            scores=[80, 80],
        ),
    ],
)


async def test_progress_empty(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("/progress")
    assert api.calls[-1] == ("get_progress", {"user_id": api.user_id})
    assert tg.last_text() == render.PROGRESS_EMPTY
    assert tg.of(SendMessage)[-1].reply_markup is None


async def test_progress_mixed(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    api.progress = MIXED
    await user.send("/progress")
    text = tg.last_text()
    assert "🔴 <b>Слабые темы</b>\n• Индексы и планы запросов — ≈ 45 · 3 попытки" in text
    # не больше двух ссылок, одной строкой
    assert text.count("<a href=") == 2
    assert "Use The Index" not in text
    assert "• python.asyncio — ≈ 40 · 1 попытка, похоже стоит повторить" in text
    assert "• web.http — ≈ 75 · 1 попытка\n" in text
    assert "🟢 <b>В порядке:</b> SQL ≈ 80 (4)" in text
    assert "02.11 · Python-разработчик · 3 из 5 пройдено · ≈ 60" in text
    assert "01.11 · Повторение · 2 из 2 пройдено · ≈ 80" in text
    assert text.endswith(render.PROGRESS_NOTE)
    markup = tg.of(SendMessage)[-1].reply_markup
    assert markup.inline_keyboard[0][0].callback_data == RepeatCb().pack()


def test_attempts_word() -> None:
    assert [render.attempts_word(n) for n in (1, 2, 5, 11, 21, 22, 25)] == [
        "1 попытка",
        "2 попытки",
        "5 попыток",
        "11 попыток",
        "21 попытка",
        "22 попытки",
        "25 попыток",
    ]


def test_progress_many_topics_cut() -> None:
    progress = Progress(topics=[tp(f"x.t{i}", "weak", 30, 2) for i in range(13)], sessions=[])
    text = render.progress_text(progress)
    assert text.count("• x.t") == 10
    assert "и ещё 3" in text


async def test_repeat(user: Chat_, api: FakeApi, tg: FakeSession, store: StateStore) -> None:
    await user.send("Ищем Python-разработчика")
    await user.send("/repeat")
    assert api.calls[-1] == ("create_repeat", {"user_id": api.user_id, "n": 3})
    assert tg.of(SendMessage)[-2].text == render.PREPARING_REPEAT
    assert tg.last_text().startswith("🔁 Повторение · <b>Вопрос 1 из 2</b>")
    state = await store.get_user(USER)
    assert state.vacancy_id == api.vacancy.id  # вакансия не теряется
    assert state.mode == "answer"

    await user.send("/repeat 20")
    assert "от 3 до 15" in tg.last_text()


async def test_repeat_nothing(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    api.errors["create_repeat"] = ApiError(409, "nothing_to_repeat", "нет")
    await user.press(RepeatCb().pack())
    assert tg.last_text() == render.NOTHING_TO_REPEAT


async def test_progress_button(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.press(ProgressCb().pack())
    assert tg.last_text() == render.PROGRESS_EMPTY


def _with_topics(api: FakeApi) -> None:
    api.vacancy = api.vacancy.model_copy(
        update={
            "requirements": [
                Requirement(
                    id=uuid.uuid4(),
                    text="PostgreSQL",
                    is_required=True,
                    topic="db.indexes",
                    topic_title="Индексы и планы запросов",
                ),
                Requirement(
                    id=uuid.uuid4(),
                    text="Транзакции",
                    is_required=True,
                    topic="db.indexes",
                    topic_title="Индексы и планы запросов",
                ),
                Requirement(
                    id=uuid.uuid4(),
                    text="asyncio",
                    is_required=False,
                    topic="python.asyncio",
                    topic_title="asyncio и асинхронный код",
                ),
            ]
        }
    )


async def test_pick_topics(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    _with_topics(api)
    api.progress = Progress(topics=[tp("db.indexes", "weak", 40, 2)], sessions=[])
    await user.send("Ищем Python-разработчика")
    tg.clear()
    await user.press(TopicsCb(action="open", vacancy_id=api.vacancy.id).pack())
    sent = tg.of(SendMessage)[-1]
    rows = sent.reply_markup.inline_keyboard
    assert [r[0].text for r in rows[:-2]] == [
        "⬜ 🔴 Индексы и планы запросов",
        "⬜ asyncio и асинхронный код",
    ]
    assert [b.text for b in rows[-2]] == ["▶️ 5 вопросов", "10 вопросов", "15 вопросов"]

    await user.press(TopicsCb(action="start", i=5).pack())
    assert tg.of(AnswerCallbackQuery)[-1].text == render.PICK_NONE
    assert "create_session" not in api.names()

    await user.press(TopicsCb(action="toggle", i=1).pack())
    rows = tg.of(EditMessageText)[-1].reply_markup.inline_keyboard
    assert rows[1][0].text == "✅ asyncio и асинхронный код"
    await user.press(TopicsCb(action="toggle", i=0).pack())
    await user.press(TopicsCb(action="toggle", i=0).pack())  # снять отметку

    await user.press(TopicsCb(action="start", i=10).pack())
    assert api.calls[-1] == (
        "create_session",
        {"vacancy_id": api.vacancy.id, "n": 10, "topics": ["python.asyncio"]},
    )
    assert tg.last_text().startswith("<b>Вопрос 1 из 10</b>")


async def test_pick_survives_reopen(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    """Замечание автора: закрыл список, открыл снова — отметки на месте (007 §8.3)."""
    _with_topics(api)
    await user.send("Ищем Python-разработчика")
    await user.press(TopicsCb(action="open", vacancy_id=api.vacancy.id).pack())
    await user.press(TopicsCb(action="toggle", i=0).pack())
    await user.press(TopicsCb(action="open", vacancy_id=api.vacancy.id).pack())
    rows = tg.of(SendMessage)[-1].reply_markup.inline_keyboard
    assert rows[0][0].text == "✅ Индексы и планы запросов"

    # после старта тренировки отметки сброшены
    await user.press(TopicsCb(action="start", i=5).pack())
    await user.send("/new")
    await user.send("Ищем Python-разработчика")
    await user.press(TopicsCb(action="open", vacancy_id=api.vacancy.id).pack())
    rows = tg.of(SendMessage)[-1].reply_markup.inline_keyboard
    assert rows[0][0].text == "⬜ Индексы и планы запросов"

    # другая вакансия — отметки прежней не переносятся
    await user.press(TopicsCb(action="toggle", i=0).pack())
    api.vacancy = api.vacancy.model_copy(update={"id": uuid.uuid4()})
    await user.press(TopicsCb(action="open", vacancy_id=api.vacancy.id).pack())
    rows = tg.of(SendMessage)[-1].reply_markup.inline_keyboard
    assert rows[0][0].text == "⬜ Индексы и планы запросов"


async def test_skip_shows_docs(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("Ищем Python-разработчика")
    await user.send("/train")
    assert api.session is not None
    first, *rest = api.session.questions
    api.session = api.session.model_copy(
        update={"questions": [first.model_copy(update={"docs": DOCS[:2]}), *rest]}
    )
    await user.press(SkipCb(question_id=first.id).pack())
    texts = tg.texts()
    assert texts[-2].startswith("⏭ Пропущено.\n📚 Почитать: <a href=")
    assert texts[-2].count("<a href=") == 2
    assert texts[-1].startswith("<b>Вопрос 2 из 3</b>")

    await user.press(SkipCb(question_id=rest[0].id).pack())  # без ссылок — без сообщения
    assert not tg.texts()[-2].startswith("⏭")


async def test_pick_cancel_and_no_topics(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("Ищем Python-разработчика")
    await user.press(TopicsCb(action="open", vacancy_id=api.vacancy.id).pack())
    assert tg.of(AnswerCallbackQuery)[-1].text == render.NO_TOPICS
    _with_topics(api)
    await user.press(TopicsCb(action="open", vacancy_id=api.vacancy.id).pack())
    await user.press(TopicsCb(action="cancel").pack())
    assert tg.last_text() == render.PICK_CANCELLED


async def test_docs_under_weak_evaluation(
    user: Chat_, api: FakeApi, tg: FakeSession, trainer: Trainer
) -> None:
    await user.send("Ищем Python-разработчика")
    await user.send("/train")
    await user.send("ответ")
    weak = api.last_answer()
    api.evaluate(weak.id, total=30, passed=False, docs=DOCS)
    await trainer.on_event(weak.id, "done")
    text = next(t for t in tg.texts() if t.startswith("<b>🔴 Слабо"))
    assert '📚 Почитать: <a href="https://postgrespro.ru/docs/postgresql/17/indexes">' in text
    assert text.count("<a href=") == 2

    await user.send("ответ 2")
    good = api.last_answer()
    api.evaluate(good.id, total=80, passed=True, docs=DOCS)
    await trainer.on_event(good.id, "done")
    text = next(t for t in tg.texts() if t.startswith("<b>🟢 Хорошо"))
    assert "Почитать" not in text


async def test_forget_all(
    user: Chat_, api: FakeApi, tg: FakeSession, store: StateStore, trainer: Trainer
) -> None:
    await user.send("Ищем Python-разработчика")
    await user.send("/train")
    await user.send("ответ")
    assert len(await store.all_pending()) == 1

    await user.send("/forget")
    await user.press(ForgetCb(action="all").pack())
    assert tg.last_text() == render.FORGET_ALL_CONFIRM
    assert "forget_all" not in api.names()
    await user.press(ForgetCb(action="all_confirm").pack())
    assert api.calls[-1] == ("forget_all", {"user_id": api.user_id})
    assert tg.last_text().startswith("Удалено: вакансий 1, ответов 7, голосовых 2.")
    state = await store.get_user(USER)
    assert (state.user_id, state.vacancy_id, state.session_id, state.mode) == (
        api.user_id,
        None,
        None,
        "idle",
    )
    assert await store.all_pending() == []
    await user.send("/train")
    assert tg.last_text() == render.NO_VACANCY


async def test_forget_all_error(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    api.errors["forget_all"] = ApiError(503, "storage_unavailable", "s3")
    await user.press(ForgetCb(action="all_confirm").pack())
    assert tg.last_text() == render.API_ERRORS["storage_unavailable"]


async def test_help_mentions_new_commands(user: Chat_, tg: FakeSession) -> None:
    await user.send("/help")
    assert "/progress" in tg.last_text() and "/repeat" in tg.last_text()
