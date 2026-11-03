"""Тексты и подписи веб-кабинета (009 §6). По смыслу те же, что в боте; бот не импортируется.

Здесь только простой текст: HTML собирают шаблоны, Jinja всё экранирует.
"""

from sobesnik_web.api import ApiError, TopicProgress

GOOD_FROM = 75
APPROX_STEP = 5
DOUBTS_SHOWN = 3
SLOW_AFTER_S = 60
"""Через сколько секунд статус дополняется «модель думает дольше обычного» (BOT-6)."""
QUESTIONS_MIN, QUESTIONS_MAX, QUESTIONS_DEFAULT = 3, 15, 5
ANSWER_MAX_CHARS = 5_000
VACANCY_MAX_CHARS = 20_000

LEVEL_TITLES = {
    "intern": "Intern",
    "junior": "Junior",
    "middle": "Middle",
    "senior": "Senior",
    "lead": "Lead",
}
SHORT_TITLES = {"terminology": "Термины"}

APPROX_NOTE = "Балл примерный, ±20: модель небольшая. Ориентируйтесь на упущенное и совет."
PROGRESS_NOTE = (
    "Баллы примерные: одна оценка ошибается на ±20, поэтому смотрим среднее последних трёх попыток."
)
PRIVACY_NOTE = (
    "Всё хранится только на этом компьютере: вакансии, ответы и записи голоса не уходят "
    "в интернет. Записи удаляются сами через 30 дней или сразу — в «Мои данные»."
)

LINK_INVALID = "Ссылка или код недействительны. Возьмите новые командой /web в боте."
CODE_SLOW_DOWN = (
    "Слишком много неверных кодов подряд. Подождите несколько секунд и попробуйте снова."
)
NOTHING_TO_REPEAT = "Слабых тем нет 🎉 Пройдите тренировку по вакансии."
NO_SOURCE = "Вставьте текст вакансии, ссылку на неё или выберите сохранённую страницу (.html)."
EMPTY_ANSWER = "Напишите ответ или запишите голос."

FAILURES = {
    "asr_empty": "🔇 Не расслышал речь. Запишите ещё раз или ответьте текстом.",
    "audio_unreadable": "Не получилось прочитать запись. Запишите ещё раз или ответьте текстом.",
    "audio_missing": "Запись не нашлась в хранилище. Запишите ещё раз или ответьте текстом.",
    "asr_failed": "Распознавание речи не сработало. Попробуйте ещё раз или ответьте текстом.",
    "no_transcript": "Распознавание речи не сработало. Попробуйте ещё раз или ответьте текстом.",
    "llm_unavailable": "Модель так и не ответила. Ответ сохранён — проверьте, что Ollama "
    "запущена, и обновите страницу позже.",
    "llm_model_missing": "Модель для оценки не скачана. На этом компьютере выполните "
    "«ollama pull» с моделью из .env (по умолчанию qwen3.5:4b).",
    "llm_bad_output": "Модель ответила невнятно. Попробуйте ответить ещё раз.",
    "llm_error": "Модель ответила ошибкой. Попробуйте ответить ещё раз.",
}
"""Почему ответ не оценён (`answer.error`)."""

API_ERRORS = {
    "llm_unavailable": "Модель сейчас недоступна. Проверьте, что Ollama запущена, "
    "и попробуйте ещё раз.",
    "llm_model_missing": "Модель не скачана. На этом компьютере выполните «ollama pull» "
    "с моделью из .env (по умолчанию qwen3.5:4b).",
    "llm_bad_output": "Модель ответила невнятно. Попробуйте ещё раз.",
    "llm_error": "Модель ответила ошибкой. Попробуйте ещё раз.",
    "api_unavailable": "Сервис Sobesnik не отвечает. Проверьте: docker compose ps.",
    "already_answered": "На этот вопрос уже есть ответ.",
    "session_finished": "Тренировка уже завершена — начните новую.",
    "nothing_to_repeat": NOTHING_TO_REPEAT,
    "audio_too_large": "Запись слишком большая. Запишите короче или ответьте текстом.",
    "audio_too_long": "Запись длиннее 5 минут. Запишите короче или ответьте текстом.",
    "audio_too_short": "Запись короче секунды. Запишите ещё раз или ответьте текстом.",
    "audio_unsupported": "Не понимаю этот формат записи. Подойдут ogg, webm, mp3, wav, m4a.",
    "storage_unavailable": "Не получилось сохранить запись. Ответьте текстом или попробуйте позже.",
    "validation_error": "Слишком длинный или пустой текст.",
    "link_invalid": LINK_INVALID,
    "local_login_disabled": "Вход без Telegram выключен (WEB_LOCAL_LOGIN=false в .env).",
    "unauthorized": "Сервисный токен веба не подходит к API: проверьте API_SERVICE_TOKEN в .env.",
}
HUMAN_MESSAGE_CODES = frozenset(
    {"not_a_vacancy", "url_unreadable", "url_not_allowed", "html_unreadable"}
)
"""Ошибки разбора вакансии API уже пишет по-человечески."""


def api_error_text(error: ApiError) -> str:
    if error.hint:  # API знает точнее: какая модель, какой адрес (008 §7)
        return error.hint
    if error.code in API_ERRORS:
        return API_ERRORS[error.code]
    if error.code in HUMAN_MESSAGE_CODES:
        return error.message[:1].upper() + error.message[1:]
    return f"Что-то пошло не так (код: {error.code})."


def failure_text(code: str | None) -> str:
    return FAILURES.get(code or "", f"Не получилось оценить ответ (код: {code or '?'}).")


def band(total: int, passed: bool) -> str:
    if not passed:
        return "🔴 Слабо"
    return "🟢 Хорошо" if total >= GOOD_FROM else "🟡 Средне"


def approx(total: float) -> int:
    """Итог, округлённый до 5: точнее модель всё равно не ставит (±20)."""
    return int(APPROX_STEP * round(total / APPROX_STEP))


def dots(score: int) -> str:
    score = max(0, min(3, score))
    return "●" * score + "○" * (3 - score)


def criterion_title(criterion: str, title: str) -> str:
    return SHORT_TITLES.get(criterion, title)


def attempts_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} попытка"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} попытки"
    return f"{n} попыток"


def topic_score(topic: TopicProgress) -> str:
    return f"≈ {approx(topic.avg_last3)}" if topic.avg_last3 is not None else "—"


def status_line(kind: str, status: str) -> str:
    """Этап обработки ответа (BOT-6): голос начинается с распознавания, текст — с оценки."""
    if kind == "voice" and status in ("received", "transcribing"):
        return "🎧 Распознаю речь…"
    return "🧠 Оцениваю ответ…"


def forgotten_text(
    audio: int, transcripts: int, *, vacancies: int | None = None, answers: int | None = None
) -> str:
    if vacancies is not None:
        return (
            f"Удалено: вакансий {vacancies}, ответов {answers or 0}, голосовых {audio}. "
            "Начнём заново — добавьте вакансию."
        )
    if audio == 0:
        return "Голосовых записей нет — удалять нечего."
    return f"Удалено записей: {audio}, расшифровок: {transcripts}."
