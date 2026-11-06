"""Тексты и клавиатуры бота (006 §4, §8, 007 §8). Чистые функции: ответы API → HTML Telegram.

Здесь только отображение. Итог, «пройден», сомнения в распознавании считает API.
"""

import uuid
from html import escape

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from sobesnik_bot.api import (
    LEVELS,
    Answer,
    ApiError,
    Difficulty,
    Doc,
    Level,
    Progress,
    Review,
    Session,
    SessionQuestion,
    Summary,
    TopicProgress,
    Vacancy,
)

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
    n: int = 0
    """0 — число по умолчанию (BOT_QUESTIONS) и продолжение незавершённой сессии."""


MORE_QUESTIONS = (10, 15)
"""Кнопки длинной тренировки под «Начать»; любое число от 3 до 15 — /train N."""


class SkipCb(CallbackData, prefix="skip"):
    question_id: uuid.UUID


class DetailsCb(CallbackData, prefix="why"):
    answer_id: uuid.UUID


class ReviewCb(CallbackData, prefix="rev"):
    answer_id: uuid.UUID


class NewVacancyCb(CallbackData, prefix="newvac"):
    pass


class ForgetCb(CallbackData, prefix="forget"):
    action: str
    """`audio`, `all` (спросить ещё раз), `all_confirm`, `cancel` (007 §8.5)."""


class TopicsCb(CallbackData, prefix="topics"):
    """Выбор тем перед тренировкой (007 §8.3). Номер темы — индекс в списке тем вакансии:
    UUID и slug вместе не влезают в 64 байта `callback_data`."""

    action: str
    """`open`, `toggle`, `start`, `cancel`."""
    vacancy_id: uuid.UUID | None = None
    i: int = 0
    """Для `toggle` — номер темы, для `start` — число вопросов."""


class SettingsCb(CallbackData, prefix="opts"):
    """Настройки тренировки (010 §5.1)."""

    action: str
    """`open`, `required` (переключить), `easier`, `same`, `harder`, `done`."""


class ProgressCb(CallbackData, prefix="progress"):
    pass


class RepeatCb(CallbackData, prefix="repeat"):
    force: bool = False
    """«Всё равно повторить»: сроки не подошли — темы с ближайшим сроком (011 §5)."""


BUTTON_NEW = "📄 Новая вакансия"
BUTTON_TRAIN = "▶️ Тренировка"
BUTTON_PROGRESS = "📊 Прогресс"
BUTTON_REPEAT = "🔁 Повторить"
PANEL_HINT = "Внизу теперь панель с кнопками: новая вакансия, тренировка, прогресс и повторение."


def _plain(text: str) -> str:
    """Без краёв и `U+FE0F`: промах — и текст панели ушёл бы модели как ответ (012 §4.2)."""
    return text.replace("\ufe0f", "").strip()


PANEL_COMMANDS = {
    _plain(BUTTON_NEW): "new",
    _plain(BUTTON_TRAIN): "train",
    _plain(BUTTON_PROGRESS): "progress",
    _plain(BUTTON_REPEAT): "repeat",
}


def panel_command(text: str | None) -> str | None:
    """Команда кнопки панели: `new`, `train`, `progress`, `repeat`; другой текст — `None`."""
    return PANEL_COMMANDS.get(_plain(text)) if text else None


def panel() -> ReplyKeyboardMarkup:
    """Постоянная панель (012 §3). Telegram не даёт редактировать сообщение с такой
    клавиатурой, поэтому она только на сообщениях, которые бот не правит."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_NEW), KeyboardButton(text=BUTTON_TRAIN)],
            [KeyboardButton(text=BUTTON_PROGRESS), KeyboardButton(text=BUTTON_REPEAT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


INTRO = (
    "Привет! Я тренирую к техническим собеседованиям.\n\n"
    "1. Пришлите текст вакансии или ссылку на неё — я разберу стек и уровень.\n"
    "2. Нажмите «Начать тренировку» — задам вопросы по вакансии.\n"
    "3. Отвечайте текстом, голосовым или кружочком (берётся только звук). "
    "Оценка — через 15–60 секунд.\n\n"
    "Оценку ставит небольшая локальная модель: балл примерный (±20), "
    "смотрите на упущенные пункты и совет.\n"
    "Голосовые проходят через серверы Telegram, на этом инстансе хранятся 30 дней. "
    "Удалить их раньше — /forget. Совсем без Telegram — веб-кабинет на компьютере "
    "с Sobesnik: /web."
)

HELP = (
    INTRO + "\n\nКоманды:\n"
    "/new — новая вакансия\n"
    "/train — тренировка по текущей вакансии (/train 10 — из 10 вопросов, от 3 до 15)\n"
    "/progress — темы: где слабо, где в порядке, что почитать\n"
    "/repeat — новые вопросы по темам, которые пора повторить\n"
    "/forget — удалить голосовые или все мои данные\n"
    "/web — войти в веб-кабинет на компьютере с Sobesnik\n"
    "/help — эта справка\n\n"
    "Кнопки внизу экрана — те же /new, /train, /progress и /repeat."
)

ASK_VACANCY = "Пришлите текст вакансии или ссылку на неё."
NO_VACANCY = "Сначала пришлите вакансию: текст или ссылку."
VOICE_NOT_NOW = "Голосом отвечают на вопрос тренировки. Сначала вакансия, потом /train."
VOICE_RECEIVING = "✅ Получил, скачиваю…"
DOWNLOAD_FAILED = "Не получилось скачать файл из Telegram. Отправьте ещё раз."
WAIT_EVALUATION = "Жду оценку предыдущего ответа — пришлю её и следующий вопрос."


def not_allowed(telegram_id: int | None) -> str:
    """Отказ закрытого инстанса (BOT-5) с id: владелец добавит его в allowlist (008 §7)."""
    if telegram_id is None:
        return "Это закрытый Sobesnik."
    return (
        f"Это закрытый Sobesnik. Ваш telegram_id: {telegram_id} — владелец может добавить его "
        "в BOT_ALLOWED_IDS."
    )


def llm_warning(hint: str | None) -> str:
    """Предупреждение к /start, если модель не готова (008 §7)."""
    return "⚠️ " + escape(hint or "Модель сейчас недоступна — вакансию пока не разобрать.")


FILE_TOO_BIG = (
    "Файл больше 20 МБ — Telegram не отдаёт боту такие. Запишите короче или ответьте текстом."
)
PARSING = "📄 Разбираю вакансию… обычно 30–50 секунд."
BUSY = "⏳ Ещё работаю над предыдущим запросом — дождитесь ответа."
VACANCY_TOO_LONG = (
    "Вакансия длиннее 20 000 символов. Оставьте главное: требования, стек и обязанности."
)
ANSWER_TOO_LONG = "Ответ длиннее 5 000 символов — сократите и пришлите ещё раз."
PRIVATE_ONLY = "Я работаю только в личных сообщениях."
FORGET_ASK = (
    "Что удалить?\n"
    "• <b>Только голос</b> — записи и расшифровки, оценки останутся.\n"
    "• <b>Всё</b> — вакансии, ответы, оценки и прогресс.\n"
    "Отменить удаление будет нельзя."
)
FORGET_ALL_CONFIRM = "Удалить вакансии, ответы, оценки и прогресс? Отменить нельзя."
FORGET_CANCELLED = "Ничего не удалено."
NOTHING_TO_REPEAT = "Повторять пока нечего — сначала ответьте на пару вопросов: /train."
NOTHING_DUE = "Сегодня повторять нечего 🎉 Сроки — в /progress."
PREPARING_REPEAT = "🧩 Готовлю вопросы для повторения… обычно около 40 секунд."
PICK_NONE = "Выберите хотя бы одну тему."
PICK_CANCELLED = "Выбор тем отменён."
NO_TOPICS = "У требований вакансии нет тем — тренируйтесь по всей вакансии."
SKIPPED = "Вопрос пропущен."


def web_link_text(web_url: str, secret: str, code: str) -> str:
    """Вход в веб-кабинет (009 §5.2). Ссылка ведёт на этот компьютер: на телефоне она не
    откроется — для него код. Превью ссылок у бота выключено: предпросмотр её не тратит."""
    base = web_url.rstrip("/")
    return (
        "🌐 <b>Веб-кабинет</b> — откройте на компьютере, где запущен Sobesnik:\n"
        f"{escape(base)}/login/{escape(secret)}\n\n"
        f"Telegram на телефоне? Откройте на компьютере {escape(base)}/login и введите код: "
        f"<code>{escape(code)}</code>\n\n"
        "Ссылка и код работают один раз, 10 минут. Не пересылайте их: это вход в ваш профиль."
    )


def preparing(n: int, options: str = "") -> str:
    extra = f", {options}" if options else ""
    return f"🧩 Готовлю вопросы ({n}{extra})… обычно около 40 секунд."


def forgotten_all(vacancies: int, answers: int, audio: int) -> str:
    return (
        f"Удалено: вакансий {vacancies}, ответов {answers}, голосовых {audio}.\n"
        "Начнём заново — пришлите вакансию."
    )


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
    more = [
        InlineKeyboardButton(
            text=f"{n} вопросов", callback_data=TrainCb(vacancy_id=vacancy.id, n=n).pack()
        )
        for n in MORE_QUESTIONS
    ]
    pick = InlineKeyboardButton(
        text="🎯 Выбрать темы",
        callback_data=TopicsCb(action="open", vacancy_id=vacancy.id).pack(),
    )
    settings = InlineKeyboardButton(
        text="⚙️ Настройки", callback_data=SettingsCb(action="open").pack()
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[levels[:3], levels[3:], [start], more, [pick, settings]]
    )


# --- настройки тренировки (010 §5) ---------------------------------------------

DIFFICULTY_TITLES: dict[Difficulty, str] = {
    "easier": "проще",
    "same": "как в вакансии",
    "harder": "сложнее",
}
SETTINGS_TEXT = (
    "<b>⚙️ Настройки тренировки</b>\n"
    "Действуют на все тренировки и /repeat, пока не смените.\n\n"
    "• <b>Только обязательные</b> — вопросы только по требованиям с ✱.\n"
    "• <b>Сложность</b> — вопросы на уровень проще или сложнее, чем в вакансии."
)
NO_REQUIRED = (
    "В вакансии нет обязательных требований (✱) — снимите «Только обязательные» в ⚙️ Настройках."
)


def options_line(required_only: bool, difficulty: Difficulty) -> str:
    """Не по умолчанию — короткая строка «только обязательные · сложнее», иначе пусто."""
    parts = []
    if required_only:
        parts.append("только обязательные")
    if difficulty != "same":
        parts.append(DIFFICULTY_TITLES[difficulty])
    return " · ".join(parts)


def settings_keyboard(required_only: bool, difficulty: Difficulty) -> InlineKeyboardMarkup:
    required = InlineKeyboardButton(
        text=f"{'☑' if required_only else '☐'} Только обязательные",
        callback_data=SettingsCb(action="required").pack(),
    )
    levels = [
        InlineKeyboardButton(
            text=f"✓ {title}" if value == difficulty else title,
            callback_data=SettingsCb(action=value).pack(),
        )
        for value, title in DIFFICULTY_TITLES.items()
    ]
    done = InlineKeyboardButton(text="Готово", callback_data=SettingsCb(action="done").pack())
    return InlineKeyboardMarkup(inline_keyboard=[[required], levels, [done]])


def settings_saved(required_only: bool, difficulty: Difficulty) -> str:
    line = options_line(required_only, difficulty)
    return f"⚙️ Настройки: {line}." if line else "⚙️ Настройки: по всей вакансии, как в вакансии."


# --- выбор тем ----------------------------------------------------------------

TOPIC_BUTTON_MAX = 40


def vacancy_topics(vacancy: Vacancy) -> list[tuple[str, str]]:
    """Темы требований вакансии по порядку, без повторов: (slug, заголовок)."""
    seen: dict[str, str] = {}
    for r in vacancy.requirements:
        if r.topic and r.topic not in seen:
            seen[r.topic] = r.topic_title or r.topic
    return list(seen.items())


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


PICK_COUNTS = (5, 10, 15)
"""Число вопросов по выбранным темам — кнопками в самом списке тем."""


def pick_text() -> str:
    return (
        "<b>🎯 Выберите темы и число вопросов</b>\n"
        "Вопросы будут по требованиям вакансии с этими темами.\n"
        "<i>🔴 — слабая тема по прошлым ответам.</i>"
    )


def pick_keyboard(
    topics: list[tuple[str, str]], picked: list[str], weak: set[str]
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if slug in picked else '⬜'} {'🔴 ' if slug in weak else ''}"
                + _cut(title, TOPIC_BUTTON_MAX),
                callback_data=TopicsCb(action="toggle", i=i).pack(),
            )
        ]
        for i, (slug, title) in enumerate(topics)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=f"▶️ {n} вопросов" if n == PICK_COUNTS[0] else f"{n} вопросов",
                callback_data=TopicsCb(action="start", i=n).pack(),
            )
            for n in PICK_COUNTS
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="Отмена", callback_data=TopicsCb(action="cancel").pack())]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- вопрос ------------------------------------------------------------------


def question_topic(question: SessionQuestion) -> str | None:
    """Короткий заголовок темы; слаг — только если темы нет в справочнике."""
    return short_title(question.topic_title) if question.topic_title else question.topic


def question_text(session: Session, position: int) -> str:
    question = next(q for q in session.questions if q.position == position)
    title = question_topic(question)
    topic = f" · тема: {escape(title)}" if title else ""
    head = f"<b>Вопрос {position} из {len(session.questions)}</b>{topic}"
    if session.mode == "repeat":
        head = "🔁 Повторение · " + head
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


ASR_FIRST_TIME = "🎧 Готовлю распознавание — первый раз это пара минут, потом быстро…"


def status_text(
    kind: str,
    status: str,
    answer: Answer | None = None,
    *,
    slow: bool = False,
    asr_ready: bool = True,
) -> str:
    """Статусное сообщение ответа в работе (BOT-6, 006 §4.5); `asr_ready=False` — модель
    распознавания ещё качается (011 §4)."""
    if kind == "voice" and status in ("received", "transcribing"):
        lines = [f"✅ Принято. {'🎧 Распознаю речь…' if asr_ready else ASR_FIRST_TIME}"]
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
    if not ev.passed and answer.docs:
        lines += ["", "📚 Почитать: " + docs_line(answer.docs)]
    if answer.transcript is not None:
        lines += ["", transcript_block(answer.transcript.text)]
        doubts = doubts_block(ev.asr_doubts)
        if doubts:
            lines.append(doubts)
    return "\n".join(lines)


DOCS_SHOWN = 2
"""Ссылки — одной строкой, не больше двух (007 §8.4)."""


def skipped_text(docs: list[Doc]) -> str:
    return "⏭ Пропущено.\n📚 Почитать: " + docs_line(docs)


def docs_line(docs: list[Doc]) -> str:
    return " · ".join(
        f'<a href="{escape(d.url, quote=True)}">{escape(d.title)}</a>' for d in docs[:DOCS_SHOWN]
    )


def details_keyboard(answer_id: uuid.UUID) -> InlineKeyboardMarkup:
    why = InlineKeyboardButton(
        text="Почему такие баллы?", callback_data=DetailsCb(answer_id=answer_id).pack()
    )
    review = InlineKeyboardButton(
        text="📖 Разобрать подробно", callback_data=ReviewCb(answer_id=answer_id).pack()
    )
    return InlineKeyboardMarkup(inline_keyboard=[[why], [review]])


REVIEWING = "📖 Готовлю разбор… обычно 20–40 секунд."
REVIEW_MAX_CHARS = 4_000
"""Лимит сообщения Telegram — 4 096; блоки, которые не влезли, отбрасываются с конца."""
POINT_MARKS: dict[str, str] = {"covered": "✅", "missed": "❌"}
REVIEW_ERRORS: dict[str, str] = {
    "answer_not_evaluated": "Оценки ещё нет — разбор будет после неё.",
    "transcript_deleted": "Расшифровка этого ответа удалена — разобрать его уже нельзя.",
    "llm_bad_output": "Не получилось разобрать ответ. Попробуйте ещё раз.",
}


def review_text(review: Review) -> str:
    """Разбор (010 §4.1): эталон, пункты со статусами из оценки, неточности, ссылки из карты."""
    blocks = [
        "<b>📖 Разбор</b>",
        "<b>Как мог бы ответить сильный кандидат:</b>\n" + escape(review.reference),
    ]
    if review.points:
        lines = ["<b>По пунктам:</b>"]
        for p in review.points:
            comment = f" — {escape(p.comment)}" if p.comment else ""
            lines.append(f"{POINT_MARKS[p.status]} {escape(p.text)}{comment}")
            if p.doubt:
                doubt = escape(p.doubt)
                lines.append(f"   ⚠️ <i>Этот пункт эталона, возможно, неверен: {doubt}</i>")
        blocks.append("\n".join(lines))
    if review.mistakes:
        lines = ["<b>Неточности в ответе:</b>"]
        lines += [f"• «{escape(m.quote)}» → {escape(m.correction)}" for m in review.mistakes]
        blocks.append("\n".join(lines))
    if review.question_issue:
        blocks.append(f"⚠️ <b>В самом вопросе неточность:</b> {escape(review.question_issue)}")
    tail = "📚 Почитать: " + docs_line(review.docs) if review.docs else ""
    text = ""
    for block in blocks:
        candidate = f"{text}\n\n{block}" if text else block
        if len(candidate) + len(tail) + 2 > REVIEW_MAX_CHARS:
            break
        text = candidate
    return f"{text}\n\n{tail}" if tail else text


def review_error_text(error: ApiError) -> str:
    return REVIEW_ERRORS.get(error.code) or api_error_text(error)


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
    "llm_model_missing": "Модель для оценки не скачана. На компьютере с Sobesnik выполните "
    "<code>ollama pull</code> с моделью из .env (по умолчанию qwen3.5:4b) — и ответьте ещё раз.",
    "llm_bad_output": "Модель ответила невнятно. Попробуйте ответить ещё раз.",
    "llm_error": "Модель ответила ошибкой. Попробуйте ответить ещё раз.",
}
"""Ответ `failed` в воркере: код → текст (006 §4.4, §8.4)."""

API_ERRORS: dict[str, str] = {
    "llm_unavailable": "Модель сейчас недоступна. Попробуйте через минуту.",
    "llm_model_missing": "Модель не скачана. На компьютере с Sobesnik выполните "
    "<code>ollama pull</code> с моделью из .env (по умолчанию qwen3.5:4b).",
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
    if error.hint:  # API знает точнее: какая модель, какой адрес (008 §7)
        return escape(error.hint)
    if error.code in API_ERRORS:
        return API_ERRORS[error.code]
    if error.code in HUMAN_MESSAGE_CODES:
        return escape(error.message)
    return f"Что-то пошло не так (код: {escape(error.code)})."


# --- сводка ------------------------------------------------------------------


def summary_text(session: Session, summary: Summary) -> str:
    """Сводка (006 §8.5): без среднего балла — среднее из 3–5 шумных чисел обманчиво."""
    topics = {q.id: question_topic(q) for q in session.questions}
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
    row = [
        InlineKeyboardButton(text="Новая вакансия", callback_data=NewVacancyCb().pack()),
        InlineKeyboardButton(text="📊 Прогресс", callback_data=ProgressCb().pack()),
    ]
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
    def button(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=ForgetCb(action=action).pack())

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("🎙 Только голос", "audio"), button("🗑 Всё", "all")],
            [button("Отмена", "cancel")],
        ]
    )


def forget_all_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить всё", callback_data=ForgetCb(action="all_confirm").pack()
                ),
                InlineKeyboardButton(text="Отмена", callback_data=ForgetCb(action="cancel").pack()),
            ]
        ]
    )


# --- прогресс (007 §8.1) -----------------------------------------------------------

GROUP_SHOWN = 10
PROGRESS_NOTE = (
    "<i>Баллы примерные: одна оценка ошибается на ±20, "
    "поэтому смотрим среднее последних трёх попыток. 🔁 — когда повторить: "
    "слабую тему — сразу, хорошо отвеченную — через сутки, потом через неделю и реже.</i>"
)
PROGRESS_EMPTY = "Пока нечего показать: ответьте на пару вопросов — и здесь появятся темы."


def attempts_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} попытка"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} попытки"
    return f"{n} попыток"


def short_title(title: str) -> str:
    """Заголовок темы до двоеточия: «SQL: JOIN, агрегация…» → «SQL»."""
    return title.split(":", 1)[0].strip()


def _score(topic: TopicProgress) -> str:
    return f"≈ {approx(round(topic.avg_last3))}" if topic.avg_last3 is not None else "—"


def _more(items: list[TopicProgress]) -> str:
    rest = len(items) - GROUP_SHOWN
    return f"и ещё {rest}" if rest > 0 else ""


def nothing_due_text(progress: Progress) -> str:
    """Сроки повторения не подошли (011 §5.2, решение 1)."""
    if progress.next_due is None:
        return NOTHING_TO_REPEAT
    nearest = progress.next_due
    return (
        f"Сегодня повторять нечего 🎉 Ближайшее: «{escape(short_title(nearest.title))}» — "
        f"{nearest.due_in}. Можно повторить и сейчас."
    )


def repeat_now_keyboard() -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(
        text="🔁 Всё равно повторить", callback_data=RepeatCb(force=True).pack()
    )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def progress_text(progress: Progress) -> str:
    if not progress.topics:
        return PROGRESS_EMPTY
    weak = [t for t in progress.topics if t.status == "weak"]
    few = [t for t in progress.topics if t.status == "few"]
    ok = [t for t in progress.topics if t.status == "ok"]
    lines = ["📊 <b>Прогресс</b>"]
    if weak:
        lines += ["", "🔴 <b>Слабые темы</b>"]
        for t in weak[:GROUP_SHOWN]:
            lines.append(
                f"• {escape(t.title)} — {_score(t)} · {attempts_word(t.attempts)} · 🔁 {t.due_in}"
            )
            if t.docs:
                lines.append("   📚 " + docs_line(t.docs))
        if _more(weak):
            lines.append(_more(weak))
    if few:
        lines += ["", "⚪ <b>Мало данных</b>"]
        for t in few[:GROUP_SHOWN]:
            hint = ", похоже стоит повторить" if t.low else ""
            lines.append(
                f"• {escape(t.title)} — {_score(t)} · {attempts_word(t.attempts)}{hint}"
                f" · 🔁 {t.due_in}"
            )
        if _more(few):
            lines.append(_more(few))
    if ok:
        shown = " · ".join(
            f"{escape(short_title(t.title))} {_score(t)} ({t.attempts}, 🔁 {t.due_in})"
            for t in ok[:GROUP_SHOWN]
        )
        more = f" · {_more(ok)}" if _more(ok) else ""
        lines += ["", f"🟢 <b>В порядке:</b> {shown}{more}"]
    if progress.sessions:
        lines += ["", "<b>Последние тренировки</b>"]
        for h in progress.sessions:
            name = "Повторение" if h.mode == "repeat" else escape(h.vacancy_title or "Вакансия")
            avg = f" · ≈ {approx(round(h.avg_total))}" if h.avg_total is not None else ""
            lines.append(
                f"{h.created_at:%d.%m} · {name} · {h.passed} из {len(h.scores)} пройдено{avg}"
            )
    lines += ["", PROGRESS_NOTE]
    return "\n".join(lines)


def progress_keyboard(progress: Progress) -> InlineKeyboardMarkup | None:
    """Повторить темы, у которых подошёл срок; сроков нет — «Всё равно повторить» (011 §5.2)."""
    if progress.due_count:
        text = f"🔁 Повторить: тем пора — {progress.due_count}"
        button = InlineKeyboardButton(text=text, callback_data=RepeatCb().pack())
        return InlineKeyboardMarkup(inline_keyboard=[[button]])
    if progress.next_due is not None:
        return repeat_now_keyboard()
    return None
