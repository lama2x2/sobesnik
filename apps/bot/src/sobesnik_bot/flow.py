"""Сценарий тренировки в боте (006 §4, 007 §8): вакансия → сессия → вопрос → ответ → оценка →
сводка; прогресс, повторение слабых тем, выбор тем и удаление данных.

`Trainer` — общий для обработчиков Telegram и чтения событий: обработчики сообщают ему, что
прислал пользователь, события — что ответ сменил статус. Решения о вакансиях, вопросах и оценках
принимает API.
"""

import asyncio
import contextlib
import logging
import re
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message

from sobesnik_bot import render
from sobesnik_bot.api import Accepted, Answer, Api, ApiError, Level, Session, Vacancy
from sobesnik_bot.state import Pending, StateStore, UserState

log = logging.getLogger(__name__)

T = TypeVar("T")

TELEGRAM_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
"""Больше Telegram боту файл не отдаёт."""
HTML_MAX_BYTES = 3 * 1024 * 1024
TYPING_MAX_S = 15 * 60
"""Индикатор у ожидающего ответа гаснет не позже таймаута задачи воркера."""
FINAL = ("done", "failed")

SPLIT_FROM = 3_500
"""Сообщение не короче — возможно, начало текста, который Telegram порезал по 4 096 (006 §4.6)."""
GLUE_WAIT_S = 2.0
SEND_RETRY_DELAYS_S = (1.0, 3.0, 10.0)
"""Паузы между повторами отправки в Telegram при сбое сети: VPN моргает на секунды–минуты."""
LINK_CONTEXT_MAX = 80
"""Текста вокруг ссылки меньше минимума вакансии в API — значит, прислали ссылку, а не вакансию."""

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_TRAILING = ".,;:!?)»\"'"
"""Знаки после ссылки в тексте — не часть ссылки."""


def vacancy_source(text: str, links: Sequence[str] = ()) -> dict[str, str]:
    """Ссылка — вакансия по ссылке (VAC-1), иначе текст.

    `links` — ссылки из сущностей Telegram (там есть и `hh.ru/vacancy/1` без схемы,
    и слово-гиперссылка); без них ищется `http(s)://` в тексте. Ссылка должна быть одна,
    а текста вокруг — меньше `LINK_CONTEXT_MAX` символов.
    """
    text = text.strip()
    in_text = [u.rstrip(_TRAILING) for u in _URL.findall(text)]
    found = list(dict.fromkeys(links)) or list(dict.fromkeys(in_text))
    if len(found) != 1:
        return {"text": text}
    link = found[0]
    if len(text.replace(link, "").strip()) >= LINK_CONTEXT_MAX:
        return {"text": text}
    return {"url": link if _SCHEME.match(link) else f"https://{link}"}


@dataclass
class _Glued:
    """Части длинного текста, которые Telegram прислал отдельными сообщениями."""

    chat_id: int
    parts: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class Trainer:
    def __init__(
        self,
        bot: Bot,
        api: Api,
        store: StateStore,
        *,
        questions: int = 5,
        now: Callable[[], float] = time.time,
        typing_interval_s: float = 4.0,
        slow_after_s: float = 60.0,
        glue_wait_s: float = GLUE_WAIT_S,
        send_retry_delays_s: Sequence[float] = SEND_RETRY_DELAYS_S,
        web_url: str = "http://127.0.0.1:8080",
    ) -> None:
        self.bot = bot
        self.api = api
        self.store = store
        self.questions = questions
        self.now = now
        self.typing_interval_s = typing_interval_s
        self.slow_after_s = slow_after_s
        self.glue_wait_s = glue_wait_s
        self.send_retry_delays_s = tuple(send_retry_delays_s)
        self.web_url = web_url
        self._glued: dict[int, _Glued] = {}
        self._busy: set[int] = set()
        """Пользователи, у которых идёт разбор вакансии или генерация вопросов."""
        self._locks: defaultdict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._typing: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def close(self) -> None:
        tasks = [*self._typing.values(), *(g.task for g in self._glued.values() if g.task)]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._typing.clear()
        self._glued.clear()

    async def wait_glued(self) -> None:
        """Дождаться склейки частей, которые ждут продолжения (для тестов)."""
        tasks = [g.task for g in self._glued.values() if g.task]
        await asyncio.gather(*tasks, return_exceptions=True)

    @contextlib.asynccontextmanager
    async def _exclusive(self, chat_id: int, telegram_id: int) -> AsyncIterator[bool]:
        """Одна долгая операция с моделью на пользователя: вторая получает «ещё работаю»."""
        if telegram_id in self._busy:
            await self._send(chat_id, render.BUSY)
            yield False
            return
        self._busy.add(telegram_id)
        try:
            yield True
        finally:
            self._busy.discard(telegram_id)

    # --- Telegram ----------------------------------------------------------

    async def _retry(self, call: Callable[[], Awaitable[T]]) -> T:
        """Telegram недоступен (сеть, VPN) — повторить через `send_retry_delays_s`; флуд-лимит —
        подождать, сколько просит Telegram. Последняя попытка — без перехвата."""
        for delay in self.send_retry_delays_s:
            try:
                return await call()
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
            except TelegramNetworkError as exc:
                log.warning("telegram unavailable, retry", extra={"error": str(exc)[:200]})
                await asyncio.sleep(delay)
        return await call()

    async def _send(
        self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> Message:
        return await self._retry(lambda: self.bot.send_message(chat_id, text, reply_markup=markup))

    async def _edit(
        self, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        """Редактирует сообщение; не вышло (удалено, слишком старое) — шлёт новое."""
        try:
            await self._retry(
                lambda: self.bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup
                )
            )
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return
            log.info("edit failed, sending new message", extra={"error": type(exc).__name__})
            await self._send(chat_id, text, markup)

    async def _edit_or_log(
        self, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        """Результат ответа: если Telegram так и не ответил, сценарий всё равно идёт дальше."""
        try:
            await self._edit(chat_id, message_id, text, markup)
        except TelegramAPIError as exc:
            log.warning("result message lost", extra={"error": type(exc).__name__})

    async def _typing_loop(self, chat_id: int, until: float) -> None:
        """`typing` гаснет через 5 с, поэтому повторяется каждые ~4 с (BOT-6)."""
        while self.now() < until:
            with contextlib.suppress(TelegramAPIError):
                await self.bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(self.typing_interval_s)

    @contextlib.asynccontextmanager
    async def typing(self, chat_id: int) -> AsyncIterator[None]:
        task = asyncio.create_task(self._typing_loop(chat_id, self.now() + TYPING_MAX_S))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _start_typing(self, pending: Pending) -> None:
        until = pending.accepted_at + TYPING_MAX_S
        if pending.answer_id in self._typing or self.now() >= until:
            return
        task = asyncio.create_task(self._typing_loop(pending.chat_id, until))
        self._typing[pending.answer_id] = task
        task.add_done_callback(lambda _: self._typing.pop(pending.answer_id, None))

    def _stop_typing(self, answer_id: uuid.UUID) -> None:
        task = self._typing.pop(answer_id, None)
        if task is not None:
            task.cancel()

    # --- пользователь и вакансия --------------------------------------------

    async def user(self, telegram_id: int) -> UserState:
        """Состояние пользователя; при первом обращении — пользователь в API (BOT-2)."""
        state = await self.store.get_user(telegram_id)
        if state.user_id is None:
            state.user_id = (await self.api.upsert_user(telegram_id)).id
            await self.store.save_user(telegram_id, state)
        return state

    async def start(self, chat_id: int, telegram_id: int, *, help: bool = False) -> None:
        try:
            await self.user(telegram_id)
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return
        text = render.HELP if help else render.INTRO
        with contextlib.suppress(ApiError):  # не узнали — не пугаем, ошибка будет на вакансии
            llm = await self.api.llm_status()
            if not llm.ok:
                text += "\n\n" + render.llm_warning(llm.hint)
        await self._send(chat_id, text)

    async def new_vacancy(self, chat_id: int, telegram_id: int) -> None:
        state = await self.store.get_user(telegram_id)
        state.session_id = state.question_id = None
        state.position, state.mode = 0, "idle"
        await self.store.save_user(telegram_id, state)
        await self._send(chat_id, render.ASK_VACANCY)

    async def vacancy(
        self,
        chat_id: int,
        telegram_id: int,
        *,
        text: str | None = None,
        url: str | None = None,
        html: str | None = None,
    ) -> None:
        async with self._exclusive(chat_id, telegram_id) as ok:
            if not ok:
                return
            status = await self._send(chat_id, render.PARSING)
            try:
                state = await self.user(telegram_id)
                assert state.user_id is not None
                async with self.typing(chat_id):
                    vacancy = await self.api.create_vacancy(
                        state.user_id, text=text, url=url, html=html
                    )
            except ApiError as exc:
                message = (
                    render.VACANCY_TOO_LONG
                    if exc.code == "validation_error"
                    else render.api_error_text(exc)
                )
                await self._edit(chat_id, status.message_id, message)
                return
        state = await self.store.get_user(telegram_id)  # пока шёл разбор, состояние могло смениться
        state.vacancy_id = vacancy.id
        state.session_id = state.question_id = None
        state.position, state.mode = 0, "idle"
        await self.store.save_user(telegram_id, state)
        log.info(
            "vacancy parsed", extra={"telegram_id": telegram_id, "vacancy_id": str(vacancy.id)}
        )
        await self._edit(
            chat_id,
            status.message_id,
            render.vacancy_text(vacancy),
            render.vacancy_keyboard(vacancy),
        )

    async def set_level(
        self, chat_id: int, message_id: int, vacancy_id: uuid.UUID, level: Level
    ) -> str | None:
        """Правка уровня кнопкой (VAC-4). Возвращает текст ошибки для всплывашки."""
        try:
            vacancy = await self.api.set_level(vacancy_id, level)
        except ApiError as exc:
            return render.api_error_text(exc)
        await self._edit(
            chat_id, message_id, render.vacancy_text(vacancy), render.vacancy_keyboard(vacancy)
        )
        return None

    # --- сообщения пользователя --------------------------------------------

    async def incoming_text(
        self, chat_id: int, telegram_id: int, text: str, links: Sequence[str] = ()
    ) -> None:
        """Текст из Telegram. Длинный текст Telegram режет на части по 4 096 символов: часть
        от `SPLIT_FROM` ждёт продолжения до `glue_wait_s`, часть короче — последняя (006 §4.6).

        До первого `await` всё синхронно: части одного текста встают в очередь по порядку.
        """
        glued = self._glued.get(telegram_id)
        if glued is None and len(text) < SPLIT_FROM:
            await self.text(chat_id, telegram_id, text, links)
            return
        if glued is None:
            glued = self._glued[telegram_id] = _Glued(chat_id)
        glued.parts.append(text)
        if glued.task is not None:
            glued.task.cancel()
            glued.task = None
        if len(text) < SPLIT_FROM:
            del self._glued[telegram_id]
            await self._flush_glued(glued, telegram_id)
        else:
            glued.task = asyncio.create_task(self._glue_later(telegram_id))

    async def _glue_later(self, telegram_id: int) -> None:
        await asyncio.sleep(self.glue_wait_s)
        glued = self._glued.pop(telegram_id, None)
        if glued is not None:
            await self._flush_glued(glued, telegram_id)

    async def _flush_glued(self, glued: _Glued, telegram_id: int) -> None:
        log.info("text glued", extra={"telegram_id": telegram_id, "parts": len(glued.parts)})
        await self.text(glued.chat_id, telegram_id, "\n".join(glued.parts))

    async def text(
        self, chat_id: int, telegram_id: int, text: str, links: Sequence[str] = ()
    ) -> None:
        """Текст — ответ на вопрос, если бот его ждёт; иначе вакансия."""
        state = await self.store.get_user(telegram_id)
        if state.mode == "waiting":
            await self._send(chat_id, render.WAIT_EVALUATION)
        elif state.mode == "answer" and state.session_id and state.question_id:
            await self._answer_text(chat_id, telegram_id, state, text)
        else:
            await self.vacancy(chat_id, telegram_id, **vacancy_source(text, links))

    async def html(self, chat_id: int, telegram_id: int, file_id: str, size: int | None) -> None:
        """Сохранённая страница вакансии файлом (VAC-1)."""
        state = await self.store.get_user(telegram_id)
        if state.mode in ("answer", "waiting"):
            await self._send(chat_id, "Сейчас идёт тренировка. Новая вакансия — /new.")
            return
        if size is not None and size > HTML_MAX_BYTES:
            await self._send(chat_id, "Страница больше 3 МБ. Пришлите текст вакансии.")
            return
        buffer = await self.bot.download(file_id)
        data = buffer.getvalue() if buffer is not None else b""  # type: ignore[attr-defined]
        await self.vacancy(chat_id, telegram_id, html=data.decode("utf-8", errors="replace"))

    async def _answer_text(
        self, chat_id: int, telegram_id: int, state: UserState, text: str
    ) -> None:
        assert state.session_id is not None and state.question_id is not None
        started = time.monotonic()
        try:
            accepted = await self.api.answer_text(state.session_id, state.question_id, text)
        except ApiError as exc:
            too_long = exc.code == "validation_error"
            await self._send(
                chat_id, render.ANSWER_TOO_LONG if too_long else render.api_error_text(exc)
            )
            return
        await self._accepted(chat_id, telegram_id, state, accepted, "text", started)

    async def voice(
        self,
        chat_id: int,
        telegram_id: int,
        *,
        file_id: str,
        size: int | None,
        filename: str,
        mime: str,
    ) -> None:
        """Голосовой ответ (С3): файл скачивается в память и уходит в API, на диск не пишется."""
        started = time.monotonic()
        state = await self.store.get_user(telegram_id)
        if state.mode == "waiting":
            await self._send(chat_id, render.WAIT_EVALUATION)
            return
        if state.mode != "answer" or not state.session_id or not state.question_id:
            await self._send(chat_id, render.VOICE_NOT_NOW)
            return
        if size is not None and size > TELEGRAM_DOWNLOAD_MAX_BYTES:
            await self._send(chat_id, render.FILE_TOO_BIG)
            return
        try:
            buffer = await self.bot.download(file_id)
        except TelegramAPIError as exc:
            log.warning("voice download failed", extra={"error": type(exc).__name__})
            await self._send(chat_id, render.FILE_TOO_BIG)
            return
        data = buffer.getvalue() if buffer is not None else b""  # type: ignore[attr-defined]
        try:
            accepted = await self.api.answer_voice(
                state.session_id, state.question_id, data, filename, mime
            )
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return
        finally:
            del data, buffer
        await self._accepted(chat_id, telegram_id, state, accepted, "voice", started)

    async def _accepted(
        self,
        chat_id: int,
        telegram_id: int,
        state: UserState,
        accepted: Accepted,
        kind: str,
        started: float,
    ) -> None:
        """Приём подтверждается сразу (BOT-4), дальше статус меняют события (BOT-6).

        `started` — `time.monotonic()` при получении сообщения: `ack_ms` в логе — замер 000 §5."""
        assert state.session_id is not None and state.question_id is not None
        message = await self._send(chat_id, render.status_text(kind, "received"))
        ack_ms = round((time.monotonic() - started) * 1000)
        pending = Pending(
            answer_id=accepted.answer_id,
            telegram_id=telegram_id,
            chat_id=chat_id,
            message_id=message.message_id,
            kind=kind,
            session_id=state.session_id,
            question_id=state.question_id,
            accepted_at=self.now(),
        )
        await self.store.add_pending(pending)
        state.mode = "waiting"
        await self.store.save_user(telegram_id, state)
        log.info(
            "answer accepted",
            extra={
                "telegram_id": telegram_id,
                "answer_id": str(accepted.answer_id),
                "kind": kind,
                "ack_ms": ack_ms,
            },
        )
        self._start_typing(pending)

    # --- сессия ------------------------------------------------------------

    async def train(
        self,
        chat_id: int,
        telegram_id: int,
        n: int | None = None,
        *,
        vacancy_id: uuid.UUID | None = None,
        new: bool = False,
        topics: list[str] | None = None,
    ) -> None:
        """Тренировка: продолжить незавершённую сессию этой вакансии или создать новую.
        С `topics` — всегда новая, только по этим темам (007 §6.1)."""
        async with self._exclusive(chat_id, telegram_id) as ok:
            if ok:
                await self._train(
                    chat_id, telegram_id, n, vacancy_id=vacancy_id, new=new, topics=topics
                )

    async def _train(
        self,
        chat_id: int,
        telegram_id: int,
        n: int | None,
        *,
        vacancy_id: uuid.UUID | None,
        new: bool,
        topics: list[str] | None = None,
    ) -> None:
        state = await self._ready(chat_id, telegram_id)
        if state is None:
            return
        if vacancy_id is not None and vacancy_id != state.vacancy_id:
            state.vacancy_id, state.session_id, new = vacancy_id, None, True
        if state.vacancy_id is None:
            await self._send(chat_id, render.NO_VACANCY)
            return
        resumable = not new and n is None and topics is None and state.session_id is not None
        if resumable and await self._resume(chat_id, telegram_id, state):
            return
        n = n or self.questions
        vacancy = state.vacancy_id
        await self._open_session(
            chat_id,
            telegram_id,
            render.preparing(n),
            lambda: self.api.create_session(vacancy, n, topics),
        )

    async def _ready(self, chat_id: int, telegram_id: int) -> UserState | None:
        """Пользователь в API и не ждёт оценку; иначе сообщение и `None`."""
        try:
            state = await self.user(telegram_id)
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return None
        if state.mode == "waiting":
            await self._send(chat_id, render.WAIT_EVALUATION)
            return None
        return state

    async def _open_session(
        self,
        chat_id: int,
        telegram_id: int,
        preparing: str,
        create: Callable[[], Awaitable[Session]],
        errors: dict[str, str] | None = None,
    ) -> None:
        status = await self._send(chat_id, preparing)
        try:
            async with self.typing(chat_id):
                session = await create()
        except ApiError as exc:
            text = (errors or {}).get(exc.code) or render.api_error_text(exc)
            await self._edit(chat_id, status.message_id, text)
            return
        # сначала состояние: если Telegram сейчас недоступен, /train продолжит эту же сессию
        state = await self.store.get_user(telegram_id)
        if session.vacancy_id is not None:  # у повторения вакансии нет — текущая остаётся
            state.vacancy_id = session.vacancy_id
        state.session_id, state.question_id = session.id, None
        state.position, state.mode, state.noted_session = 0, "idle", None
        await self.store.save_user(telegram_id, state)
        log.info(
            "session created",
            extra={"telegram_id": telegram_id, "session_id": str(session.id), "mode": session.mode},
        )
        await self._edit(
            chat_id, status.message_id, f"🧩 Готово: вопросов {len(session.questions)}."
        )
        await self._ask_next(chat_id, telegram_id, state, session)

    async def repeat(self, chat_id: int, telegram_id: int, n: int | None = None) -> None:
        """Повторение слабых тем (С5, 007 §8.2)."""
        async with self._exclusive(chat_id, telegram_id) as ok:
            if not ok:
                return
            state = await self._ready(chat_id, telegram_id)
            if state is None:
                return
            user_id = state.user_id
            assert user_id is not None
            await self._open_session(
                chat_id,
                telegram_id,
                render.PREPARING_REPEAT,
                lambda: self.api.create_repeat(user_id, n or self.questions),
                {"nothing_to_repeat": render.NOTHING_TO_REPEAT},
            )

    async def _resume(self, chat_id: int, telegram_id: int, state: UserState) -> bool:
        assert state.session_id is not None
        try:
            session = await self.api.get_session(state.session_id)
        except ApiError:
            return False
        if session.status == "finished" or session.vacancy_id != state.vacancy_id:
            return False  # и повторение: /train — это тренировка по вакансии
        if state.mode == "answer":  # задать текущий вопрос ещё раз
            state.position -= 1
        await self._ask_next(chat_id, telegram_id, state, session)
        return True

    async def _ask_next(
        self, chat_id: int, telegram_id: int, state: UserState, session: Session | None = None
    ) -> None:
        """Следующий вопрос — первый после текущего без ответа; вопросов нет — сводка."""
        assert state.session_id is not None
        try:
            session = session or await self.api.get_session(state.session_id)
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return
        upcoming = [
            q
            for q in sorted(session.questions, key=lambda q: q.position)
            if q.position > state.position and (q.answer is None or q.answer.status == "failed")
        ]
        if not upcoming:
            await self._finish(chat_id, telegram_id, state)
            return
        question = upcoming[0]
        state.question_id, state.position, state.mode = question.id, question.position, "answer"
        await self.store.save_user(telegram_id, state)
        await self._send(
            chat_id,
            render.question_text(session, question.position),
            render.skip_keyboard(question.id),
        )

    async def skip(self, chat_id: int, telegram_id: int, question_id: uuid.UUID) -> str:
        """Следующий вопрос; у пропущенного — ссылки «что почитать» по теме (007 §8.4)."""
        state = await self.store.get_user(telegram_id)
        if state.mode != "answer" or state.question_id != question_id:
            return "Этот вопрос уже не текущий."
        assert state.session_id is not None
        session = None
        with contextlib.suppress(ApiError):  # не вышло — `_ask_next` спросит API сам
            session = await self.api.get_session(state.session_id)
        questions = session.questions if session else []
        skipped = next((q for q in questions if q.id == question_id), None)
        if skipped is not None and skipped.docs:
            await self._send(chat_id, render.skipped_text(skipped.docs))
        await self._ask_next(chat_id, telegram_id, state, session)
        return render.SKIPPED

    async def _finish(self, chat_id: int, telegram_id: int, state: UserState) -> None:
        assert state.session_id is not None
        try:
            summary = await self.api.finish_session(state.session_id)
            session = await self.api.get_session(state.session_id)
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return
        vacancy_id = state.vacancy_id
        state.session_id = state.question_id = None
        state.position, state.mode = 0, "idle"
        await self.store.save_user(telegram_id, state)
        await self._send(
            chat_id, render.summary_text(session, summary), render.summary_keyboard(vacancy_id)
        )

    # --- результат ответа (события) ------------------------------------------

    async def on_event(self, answer_id: uuid.UUID, status: str) -> None:
        """Событие из Redis будит бота; сам результат — из `GET /answers/{id}` (006 §6.3)."""
        pending = await self.store.get_pending(answer_id)
        if pending is None or (status == pending.last_status and status not in FINAL):
            return
        await self.sync(answer_id)

    async def sync(self, answer_id: uuid.UUID) -> None:
        """Сверяет ожидающий ответ с API и обновляет статусное сообщение. Идемпотентна."""
        async with self._locks[answer_id]:
            pending = await self.store.get_pending(answer_id)
            if pending is None:
                return
            try:
                answer = await self.api.get_answer(answer_id)
            except ApiError as exc:
                log.warning(
                    "answer sync failed", extra={"answer_id": str(answer_id), "error": exc.code}
                )
                return
            if answer.status in FINAL:
                await self._result(pending, answer)
                return
            slow = pending.slow_noted or self.now() - pending.accepted_at >= self.slow_after_s
            if answer.status == pending.last_status and slow == pending.slow_noted:
                return
            shown = answer if answer.status == "evaluating" else None
            text = render.status_text(pending.kind, answer.status, shown, slow=slow)
            if text != render.status_text(
                pending.kind, pending.last_status, slow=pending.slow_noted
            ):
                await self._edit(pending.chat_id, pending.message_id, text)
            pending.last_status, pending.slow_noted = answer.status, slow
            await self.store.save_pending(pending)
        if answer_id not in self._typing:
            self._start_typing(pending)

    async def _result(self, pending: Pending, answer: Answer) -> None:
        if not await self.store.remove_pending(pending.answer_id):
            return  # уже обработан
        self._stop_typing(pending.answer_id)
        self._locks.pop(pending.answer_id, None)
        state = await self.store.get_user(pending.telegram_id)
        same_session = state.session_id == pending.session_id
        log.info(
            "answer result",
            extra={
                "telegram_id": pending.telegram_id,
                "answer_id": str(answer.id),
                "status": answer.status,
                "error": answer.error,
            },
        )
        if answer.status == "done" and answer.evaluation is not None:
            note = state.noted_session != pending.session_id
            await self._edit_or_log(
                pending.chat_id,
                pending.message_id,
                render.evaluation_text(answer, note=note),
                render.details_keyboard(answer.id),
            )
            if note:
                state.noted_session = pending.session_id
            if same_session and state.mode == "waiting":
                await self._ask_next(pending.chat_id, pending.telegram_id, state)
            else:
                await self.store.save_user(pending.telegram_id, state)
            return
        text = render.failure_text(answer.error)
        markup = None
        if same_session:  # тот же вопрос снова ждёт ответа: API разрешает ответ после failed
            state.mode, state.question_id = "answer", pending.question_id
            await self.store.save_user(pending.telegram_id, state)
            text += "\n\n" + render.RETRY
            markup = render.skip_keyboard(pending.question_id)
        await self._edit_or_log(pending.chat_id, pending.message_id, text, markup)

    async def resume(self) -> None:
        """После старта: вернуть индикаторы и сверить всё, что ждали до перезапуска."""
        for pending in await self.store.all_pending():
            self._start_typing(pending)
            await self.sync(pending.answer_id)

    async def reconcile(self) -> None:
        """Страховка от потерянного события: ответы, которые ждут дольше `slow_after_s`."""
        for pending in await self.store.all_pending():
            if self.now() - pending.accepted_at >= self.slow_after_s:
                await self.sync(pending.answer_id)

    async def reconcile_loop(self, interval_s: float = 30.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.reconcile()
            except Exception:
                log.exception("reconcile failed")

    # --- кнопки --------------------------------------------------------------

    async def details(self, chat_id: int, answer_id: uuid.UUID) -> None:
        try:
            answer = await self.api.get_answer(answer_id)
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return
        await self._send(chat_id, render.details_text(answer))

    async def web(self, chat_id: int, telegram_id: int) -> None:
        """`/web`: одноразовая ссылка и код входа в веб-кабинет (009 §5.2). Новая гасит старую."""
        try:
            state = await self.user(telegram_id)
            assert state.user_id is not None
            link = await self.api.web_link(state.user_id)
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return
        text = render.web_link_text(self.web_url, link.secret, link.code)
        # без превью явно, а не только по умолчанию бота: предпросмотр не должен открывать ссылку
        no_preview = LinkPreviewOptions(is_disabled=True)
        await self._retry(
            lambda: self.bot.send_message(chat_id, text, link_preview_options=no_preview)
        )

    async def forget(self, chat_id: int, telegram_id: int) -> None:
        await self._send(chat_id, render.FORGET_ASK, render.forget_keyboard())

    async def forget_action(
        self, chat_id: int, message_id: int, telegram_id: int, action: str
    ) -> None:
        """`/forget` (С6, 007 §8.5): только голос — сразу, всё — после второго подтверждения."""
        if action == "all":
            await self._edit(
                chat_id, message_id, render.FORGET_ALL_CONFIRM, render.forget_all_keyboard()
            )
            return
        if action not in ("audio", "all_confirm"):
            await self._edit(chat_id, message_id, render.FORGET_CANCELLED)
            return
        try:
            state = await self.user(telegram_id)
            assert state.user_id is not None
            if action == "audio":
                result = await self.api.forget_audio(state.user_id)
            else:
                result = await self.api.forget_all(state.user_id)
        except ApiError as exc:
            await self._edit(chat_id, message_id, render.api_error_text(exc))
            return
        if action == "audio":
            log.info(
                "audio forgotten",
                extra={"telegram_id": telegram_id, "audio": result.audio_deleted},
            )
            text = render.forgotten(result.audio_deleted, result.transcripts_deleted)
        else:
            for pending in await self.store.all_pending():
                if pending.telegram_id == telegram_id:
                    self._stop_typing(pending.answer_id)
                    await self.store.remove_pending(pending.answer_id)
            await self.store.save_user(telegram_id, UserState(user_id=state.user_id))
            log.info("user data forgotten", extra={"telegram_id": telegram_id})
            text = render.forgotten_all(
                result.vacancies_deleted, result.answers_deleted, result.audio_deleted
            )
        await self._edit(chat_id, message_id, text)

    # --- прогресс и выбор тем (007 §8) ------------------------------------------

    async def progress(self, chat_id: int, telegram_id: int) -> None:
        try:
            state = await self.user(telegram_id)
            assert state.user_id is not None
            progress = await self.api.get_progress(state.user_id)
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return
        await self._send(
            chat_id, render.progress_text(progress), render.progress_keyboard(progress)
        )

    async def _weak_topics(self, state: UserState) -> set[str]:
        assert state.user_id is not None
        try:
            progress = await self.api.get_progress(state.user_id)
        except ApiError:
            return set()
        return {t.slug for t in progress.topics if t.status == "weak"}

    async def _show_pick(
        self, chat_id: int, message_id: int | None, state: UserState, vacancy: Vacancy
    ) -> None:
        topics = render.vacancy_topics(vacancy)
        weak = await self._weak_topics(state)
        markup = render.pick_keyboard(topics, state.picked, weak)
        text = render.pick_text()
        if message_id is None:
            await self._send(chat_id, text, markup)
        else:
            await self._edit(chat_id, message_id, text, markup)

    async def pick_open(self, chat_id: int, telegram_id: int, vacancy_id: uuid.UUID) -> str | None:
        """Список тем вакансии отдельным сообщением. Возвращает текст всплывашки или `None`."""
        try:
            state = await self.user(telegram_id)
            vacancy = await self.api.get_vacancy(vacancy_id)
        except ApiError as exc:
            return render.api_error_text(exc)
        if not render.vacancy_topics(vacancy):
            return render.NO_TOPICS
        if vacancy_id != state.vacancy_id:
            state.vacancy_id, state.session_id, state.question_id = vacancy_id, None, None
            state.position, state.mode, state.picked = 0, "idle", []
        # отметки живут до старта тренировки или смены вакансии (007 §8.3)
        slugs = {slug for slug, _ in render.vacancy_topics(vacancy)}
        state.picked = [t for t in state.picked if t in slugs]
        await self.store.save_user(telegram_id, state)
        await self._show_pick(chat_id, None, state, vacancy)
        return None

    async def pick_toggle(
        self, chat_id: int, message_id: int, telegram_id: int, index: int
    ) -> str | None:
        state = await self.store.get_user(telegram_id)
        if state.vacancy_id is None:
            return render.NO_VACANCY
        try:
            vacancy = await self.api.get_vacancy(state.vacancy_id)
        except ApiError as exc:
            return render.api_error_text(exc)
        topics = render.vacancy_topics(vacancy)
        if not 0 <= index < len(topics):
            return "Список тем устарел — откройте его заново."
        slug = topics[index][0]
        if slug in state.picked:
            state.picked = [t for t in state.picked if t != slug]
        else:
            state.picked = [*state.picked, slug]
        await self.store.save_user(telegram_id, state)
        await self._show_pick(chat_id, message_id, state, vacancy)
        return None

    async def has_picked(self, telegram_id: int) -> bool:
        return bool((await self.store.get_user(telegram_id)).picked)

    async def pick_start(
        self, chat_id: int, message_id: int, telegram_id: int, n: int | None = None
    ) -> str | None:
        state = await self.store.get_user(telegram_id)
        if not state.picked:
            return render.PICK_NONE
        picked, state.picked = state.picked, []
        await self.store.save_user(telegram_id, state)
        await self._edit(chat_id, message_id, f"🎯 Темы выбраны: {len(picked)}.")
        await self.train(chat_id, telegram_id, n, new=True, topics=picked)
        return None

    async def pick_cancel(self, chat_id: int, message_id: int, telegram_id: int) -> None:
        state = await self.store.get_user(telegram_id)
        state.picked = []
        await self.store.save_user(telegram_id, state)
        await self._edit(chat_id, message_id, render.PICK_CANCELLED)
