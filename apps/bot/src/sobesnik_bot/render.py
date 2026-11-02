"""Тексты и клавиатуры бота (006 §4, §8). Чистые функции: ответы API → HTML-сообщения Telegram.

Здесь только отображение. Итог, «пройден», сомнения в распознавании считает API.
"""

import uuid
from html import escape

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sobesnik_bot.api import LEVELS, Answer, ApiError, Level, Session, Summary, Vacancy

GOOD_FROM = 75
"""С этого итога пройденный ответ показывается как «Хорошо» (только отображение)."""
TRANSCRIPT_MAX_CHARS = 3_500
"""Лимит сообщения Telegram — 4 096 символов, остальное место — под статус и сомнения."""
DOUBTS_SHOWN = 3
APPROX_STEP = 5

LEVEL_TITLES: dict[Level, str] = {
    "intern": "Intern",
    "junior": "Junior",
    "middle": "Middle",
    "senior": "Senior",
    "lead": "Lead",
}


class LevelCb(CallbackData, prefix="lvl"):
    vacancy_id: uuid.UUID
    level: Level


class TrainCb(CallbackData, prefix="train"):
    vacancy_id: uuid.UUID
    new: bool = False


class SkipCb(CallbackData, prefix="skip"):
    question_id: uuid.UUID


class DetailsCb(CallbackData, prefix="why"):
    answer_id: uuid.UUID


class NewVacancyCb(CallbackData, prefix="newvac"):
    pass


class ForgetCb(CallbackData, prefix="forget"):
    confirm: bool


INTRO = (
    "Привет! Я тренирую к техническим собеседованиям.\n\n"
    "1. Пришлите текст вакансии или ссылку на неё — я разберу стек и уровень.\n"
    "2. Нажмите «Начать тренировку» — задам вопросы по вакансии.\n"
    "3. Отвечайте текстом или голосовым. Оценка — через 15–60 секунд.\n\n"
    "Оценку ставит небольшая локальная модель: балл примерный (±20), "
    "смотрите на упущенные пункты и совет.\n"
    "Голосовые проходят через серверы Telegram, на этом инстансе хранятся 30 дней. "
    "Удалить их раньше — /forget."
)

HELP = (
    INTRO + "\n\nКоманды:\n"
    "/new — новая вакансия\n"
    "/train — тренировка по текущей вакансии (/train 3 — из 3 вопросов)\n"
    "/forget — удалить мои голосовые записи и расшифровки\n"
    "/help — эта справка"
)

ASK_VACANCY = "Пришлите текст вакансии или ссылку на неё."
NO_VACANCY = "Сначала пришлите вакансию: текст или ссылку."
VOICE_NOT_NOW = "Голосом отвечают на вопрос тренировки. Сначала вакансия, потом /train."
WAIT_EVALUATION = "Жду оценку предыдущего ответа — пришлю её и следующий вопрос."
NOT_ALLOWED = "Это закрытый инстанс Sobesnik."
FILE_TOO_BIG = (
    "Файл больше 20 МБ — Telegram не отдаёт боту такие. Запишите короче или ответьте текстом."
)
PARSING = "📄 Разбираю вакансию… обычно 30–50 секунд."
FORGET_CONFIRM = (
    "Удалить все ваши голосовые записи и их расшифровки? Оценки останутся.\n"
    "Отменить удаление будет нельзя."
)
FORGET_CANCELLED = "Ничего не удалено."
SKIPPED = "Вопрос пропущен."


def preparing(n: int) -> str:
    return f"🧩 Готовлю вопросы ({n})… обычно около 40 секунд."


def forgotten(audio: int, transcripts: int) -> str:
    if audio == 0:
        return "Голосовых записей нет — удалять нечего."
    return f"Удалено записей: {audio}, расшифровок: {transcripts}."


# --- вакансия ----------------------------------------------------------------


def vacancy_text(vacancy: Vacancy) -> str:
    level = LEVEL_TITLES.get(vacancy.level, vacancy.level)
    lines = [f"<b>{escape(vacancy.title)} ({level})</b>"]
    if vacancy.stack:
        lines.append("Стек: " + escape(", ".join(vacancy.stack)))
    if vacancy.requirements:
        lines.append("Требования:")
        lines += [
            f"• {'✱ ' if r.is_required else ''}{escape(r.text)}" for r in vacancy.requirements
        ]
        if any(r.is_required for r in vacancy.requirements):
            lines.append("<i>✱ — обязательные</i>")
    lines.append("\nУровень можно поправить кнопками.")
    return "\n".join(lines)


def vacancy_keyboard(vacancy: Vacancy) -> InlineKeyboardMarkup:
    levels = [
        InlineKeyboardButton(
            text=f"· {LEVEL_TITLES[level]} ·" if level == vacancy.level else LEVEL_TITLES[level],
            callback_data=LevelCb(vacancy_id=vacancy.id, level=level).pack(),
        )
        for level in LEVELS
    ]
    start = InlineKeyboardButton(
        text="▶️ Начать тренировку", callback_data=TrainCb(vacancy_id=vacancy.id).pack()
    )
    return InlineKeyboardMarkup(inline_keyboard=[levels[:3], levels[3:], [start]])


# --- вопрос ------------------------------------------------------------------


def question_text(session: Session, position: int) -> str:
    question = next(q for q in session.questions if q.position == position)
    topic = f" · тема: {escape(question.topic)}" if question.topic else ""
    head = f"<b>Вопрос {position} из {len(session.questions)}</b>{topic}"
    return f"{head}\n\n{escape(question.text)}\n\n<i>Ответьте текстом или голосовым.</i>"


def skip_keyboard(question_id: uuid.UUID) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(
        text="Пропустить", callback_data=SkipCb(question_id=question_id).pack()
    )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


# --- статус и оценка -----------------------------------------------------------


def _quote(text: str) -> str:
    return f"<blockquote expandable>{escape(text)}</blockquote>"


def transcript_block(text: str) -> str:
    if len(text) > TRANSCRIPT_MAX_CHARS:
        text = text[: TRANSCRIPT_MAX_CHARS - 1].rstrip() + "…"
    return "<b>Распознано:</b>\n" + _quote(text)


def doubts_block(doubts: list[str]) -> str:
    """Сомнения полезны примерно на треть (research): подаются как «проверьте», не как факт."""
    shown = doubts[:DOUBTS_SHOWN]
    if not shown:
        return ""
    body = "\n".join(f"• {d}" for d in shown)
    return "<b>Возможно, распознано неверно — проверьте:</b>\n" + _quote(body)


def status_text(kind: str, status: str, answer: Answer | None = None, *, slow: bool = False) -> str:
    """Статусное сообщение ответа в работе (BOT-6, 006 §4.5)."""
    if kind == "voice" and status in ("received", "transcribing"):
        lines = ["✅ Принято. 🎧 Распознаю речь…"]
    else:
        lines = ["✅ Принято. 🧠 Оцениваю ответ…"]
    if slow:
        lines.append("<i>Модель думает дольше обычного, ответ сохранён.</i>")
    if answer is not None and answer.transcript is not None:
        lines += ["", transcript_block(answer.transcript.text)]
    return "\n".join(lines)


def band(total: int, passed: bool) -> str:
    if not passed:
        return "🔴 Слабо"
    return "🟢 Хорошо" if total >= GOOD_FROM else "🟡 Средне"


def approx(total: int) -> int:
    """Итог, округлённый до 5: точнее модель всё равно не ставит (±20)."""
    return int(APPROX_STEP * round(total / APPROX_STEP))


def dots(score: int) -> str:
    score = max(0, min(3, score))
    return "●" * score + "○" * (3 - score)


SHORT_TITLES = {"terminology": "Термины"}


def evaluation_text(answer: Answer, *, note: bool) -> str:
    """Оценка (006 §8.2): слово и «≈ N» вместо точного числа, наверху — что можно исправить."""
    ev = answer.evaluation
    assert ev is not None
    lines = [f"<b>{band(ev.total, ev.passed)}</b> · ≈ {approx(ev.total)} из 100"]
    if note:
        lines.append(
            "<i>Балл примерный, ±20: модель небольшая. Ориентируйтесь на упущенное и совет.</i>"
        )
    lines.append(
        " · ".join(f"{SHORT_TITLES.get(s.criterion, s.title)} {dots(s.score)}" for s in ev.scores)
    )
    if ev.missed_points:
        lines += ["", "<b>Что упущено:</b>"] + [f"• {escape(p)}" for p in ev.missed_points]
    if ev.advice:
        lines += ["", f"<b>Совет:</b> {escape(ev.advice)}"]
    if answer.transcript is not None:
        lines += ["", transcript_block(answer.transcript.text)]
        doubts = doubts_block(ev.asr_doubts)
        if doubts:
            lines.append(doubts)
    return "\n".join(lines)


def details_keyboard(answer_id: uuid.UUID) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(
        text="Почему такие баллы?", callback_data=DetailsCb(answer_id=answer_id).pack()
    )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def details_text(answer: Answer) -> str:
    ev = answer.evaluation
    if ev is None:
        return "Оценки пока нет."
    lines = ["<b>Почему такие баллы</b>"]
    for s in ev.scores:
        lines += ["", f"<b>{escape(s.title)}</b> {dots(s.score)}", escape(s.reason)]
    return "\n".join(lines)


# --- ошибки --------------------------------------------------------------------

RETRY = "Ответьте ещё раз текстом или голосовым — или пропустите вопрос."

FAILURES: dict[str, str] = {
    "asr_empty": "🔇 Не расслышал речь. Запишите ещё раз или ответьте текстом.",
    "audio_unreadable": "Не получилось прочитать запись. Запишите ещё раз или ответьте текстом.",
    "audio_missing": "Запись не нашлась в хранилище. Запишите ещё раз или ответьте текстом.",
    "asr_failed": "Распознавание речи не сработало. Попробуйте ещё раз или ответьте текстом.",
    "no_transcript": "Распознавание речи не сработало. Попробуйте ещё раз или ответьте текстом.",
    "llm_unavailable": "Модель так и не ответила. Ответ сохранён — ответьте ещё раз, когда "
    "модель заработает.",
    "llm_bad_output": "Модель ответила невнятно. Попробуйте ответить ещё раз.",
    "llm_error": "Модель ответила ошибкой. Попробуйте ответить ещё раз.",
}
"""Ответ `failed` в воркере: код → текст (006 §4.4, §8.4)."""

API_ERRORS: dict[str, str] = {
    "llm_unavailable": "Модель сейчас недоступна. Попробуйте через минуту.",
    "llm_bad_output": "Модель ответила невнятно. Попробуйте ещё раз.",
    "llm_error": "Модель ответила ошибкой. Попробуйте ещё раз.",
    "api_unavailable": "Сервис временно недоступен, попробуйте позже.",
    "already_answered": "На этот вопрос уже есть ответ, жду оценку.",
    "session_finished": "Сессия уже завершена. /train — новая.",
    "audio_too_large": "Запись слишком большая. Запишите короче или ответьте текстом.",
    "audio_too_long": "Запись длиннее 5 минут. Запишите короче или ответьте текстом.",
    "audio_too_short": "Запись короче секунды. Запишите ещё раз или ответьте текстом.",
    "audio_unsupported": "Не понимаю этот формат записи. Отправьте голосовое или ответьте текстом.",
    "storage_unavailable": "Не получилось сохранить запись. Ответьте текстом или попробуйте позже.",
    "validation_error": "Слишком длинный или пустой текст.",
}
"""Ошибка вызова API: код → текст. Ошибки разбора вакансии API уже пишет по-человечески."""

HUMAN_MESSAGE_CODES = frozenset(
    {"not_a_vacancy", "url_unreadable", "url_not_allowed", "html_unreadable"}
)


def failure_text(code: str | None) -> str:
    return FAILURES.get(code or "", f"Не получилось оценить ответ (код: {escape(code or '?')}).")


def api_error_text(error: ApiError) -> str:
    if error.code in API_ERRORS:
        return API_ERRORS[error.code]
    if error.code in HUMAN_MESSAGE_CODES:
        return escape(error.message)
    return f"Что-то пошло не так (код: {escape(error.code)})."


# --- сводка ------------------------------------------------------------------


def summary_text(session: Session, summary: Summary) -> str:
    """Сводка (006 §8.5): без среднего балла — среднее из 3–5 шумных чисел обманчиво."""
    topics = {q.id: q.topic for q in session.questions}
    lines = [
        "🏁 <b>Сессия завершена</b>",
        f"Отвечено {summary.answered} из {len(summary.questions)} · зачтено {summary.passed}",
        "",
    ]
    weakest: tuple[int, str] | None = None
    for q in sorted(summary.questions, key=lambda q: q.position):
        topic = escape(topics.get(q.question_id) or "вопрос")
        if q.total is None or q.passed is None:
            mark = "пропущен" if q.answer_status is None else "без оценки"
            lines.append(f"{q.position}. — {topic}: {mark}")
            continue
        lines.append(f"{q.position}. {band(q.total, q.passed)[0]} {topic} — ≈ {approx(q.total)}")
        if weakest is None or q.total < weakest[0]:
            weakest = (q.total, topic)
    if weakest is not None and summary.evaluated > 1:
        lines += ["", f"Слабее всего: {weakest[1]}."]
    return "\n".join(lines)


def summary_keyboard(vacancy_id: uuid.UUID | None) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="Новая вакансия", callback_data=NewVacancyCb().pack())]
    if vacancy_id is not None:
        row.insert(
            0,
            InlineKeyboardButton(
                text="Новая сессия",
                callback_data=TrainCb(vacancy_id=vacancy_id, new=True).pack(),
            ),
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def forget_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Удалить", callback_data=ForgetCb(confirm=True).pack()),
                InlineKeyboardButton(text="Отмена", callback_data=ForgetCb(confirm=False).pack()),
            ]
        ]
    )
