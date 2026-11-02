"""Сценарий тренировки в боте (006 §4): вакансия → сессия → вопрос → ответ → оценка → сводка.

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
from collections.abc import AsyncIterator, Callable

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from sobesnik_bot import render
from sobesnik_bot.api import Accepted, Answer, Api, ApiError, Level, Session
from sobesnik_bot.state import Pending, StateStore, UserState

log = logging.getLogger(__name__)

TELEGRAM_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
"""Больше Telegram боту файл не отдаёт."""
HTML_MAX_BYTES = 3 * 1024 * 1024
TYPING_MAX_S = 15 * 60
"""Индикатор у ожидающего ответа гаснет не позже таймаута задачи воркера."""
FINAL = ("done", "failed")

_URL = re.compile(r"^https?://\S+$")


def vacancy_source(text: str) -> dict[str, str]:
    """Одна ссылка — вакансия по ссылке (VAC-1), иначе текст."""
    text = text.strip()
    return {"url": text} if _URL.match(text) else {"text": text}


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
    ) -> None:
        self.bot = bot
        self.api = api
        self.store = store
        self.questions = questions
        self.now = now
        self.typing_interval_s = typing_interval_s
        self.slow_after_s = slow_after_s
        self._locks: defaultdict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._typing: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def close(self) -> None:
        for task in self._typing.values():
            task.cancel()
        await asyncio.gather(*self._typing.values(), return_exceptions=True)
        self._typing.clear()

    # --- Telegram ----------------------------------------------------------

    async def _send(
        self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> Message:
        return await self.bot.send_message(chat_id, text, reply_markup=markup)

    async def _edit(
        self, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        """Редактирует сообщение; не вышло (удалено, слишком старое) — шлёт новое."""
        try:
            await self.bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup
            )
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return
            log.info("edit failed, sending new message", extra={"error": type(exc).__name__})
            await self._send(chat_id, text, markup)

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
        await self._send(chat_id, render.HELP if help else render.INTRO)

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
        status = await self._send(chat_id, render.PARSING)
        try:
            state = await self.user(telegram_id)
            assert state.user_id is not None
            async with self.typing(chat_id):
                vacancy = await self.api.create_vacancy(
                    state.user_id, text=text, url=url, html=html
                )
        except ApiError as exc:
            await self._edit(chat_id, status.message_id, render.api_error_text(exc))
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

    async def text(self, chat_id: int, telegram_id: int, text: str) -> None:
        """Текст — ответ на вопрос, если бот его ждёт; иначе вакансия."""
        state = await self.store.get_user(telegram_id)
        if state.mode == "waiting":
            await self._send(chat_id, render.WAIT_EVALUATION)
        elif state.mode == "answer" and state.session_id and state.question_id:
            await self._answer_text(chat_id, telegram_id, state, text)
        else:
            await self.vacancy(chat_id, telegram_id, **vacancy_source(text))

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
        try:
            accepted = await self.api.answer_text(state.session_id, state.question_id, text)
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return
        await self._accepted(chat_id, telegram_id, state, accepted, "text")

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
        await self._accepted(chat_id, telegram_id, state, accepted, "voice")

    async def _accepted(
        self, chat_id: int, telegram_id: int, state: UserState, accepted: Accepted, kind: str
    ) -> None:
        """Приём подтверждается сразу (BOT-4), дальше статус меняют события (BOT-6)."""
        assert state.session_id is not None and state.question_id is not None
        message = await self._send(chat_id, render.status_text(kind, "received"))
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
            extra={"telegram_id": telegram_id, "answer_id": str(accepted.answer_id), "kind": kind},
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
    ) -> None:
        """Тренировка: продолжить незавершённую сессию этой вакансии или создать новую."""
        try:
            state = await self.user(telegram_id)
        except ApiError as exc:
            await self._send(chat_id, render.api_error_text(exc))
            return
        if state.mode == "waiting":
            await self._send(chat_id, render.WAIT_EVALUATION)
            return
        if vacancy_id is not None and vacancy_id != state.vacancy_id:
            state.vacancy_id, state.session_id, new = vacancy_id, None, True
        if state.vacancy_id is None:
            await self._send(chat_id, render.NO_VACANCY)
            return
        resumable = not new and n is None and state.session_id is not None
        if resumable and await self._resume(chat_id, telegram_id, state):
            return
        n = n or self.questions
        status = await self._send(chat_id, render.preparing(n))
        try:
            async with self.typing(chat_id):
                session = await self.api.create_session(state.vacancy_id, n)
        except ApiError as exc:
            await self._edit(chat_id, status.message_id, render.api_error_text(exc))
            return
        await self._edit(
            chat_id, status.message_id, f"🧩 Готово: вопросов {len(session.questions)}."
        )
        state = await self.store.get_user(telegram_id)
        state.vacancy_id, state.session_id, state.question_id = session.vacancy_id, session.id, None
        state.position, state.mode, state.noted_session = 0, "idle", None
        await self.store.save_user(telegram_id, state)
        log.info(
            "session created", extra={"telegram_id": telegram_id, "session_id": str(session.id)}
        )
        await self._ask_next(chat_id, telegram_id, state, session)

    async def _resume(self, chat_id: int, telegram_id: int, state: UserState) -> bool:
        assert state.session_id is not None
        try:
            session = await self.api.get_session(state.session_id)
        except ApiError:
            return False
        if session.status == "finished" or session.vacancy_id != state.vacancy_id:
            return False
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
        state = await self.store.get_user(telegram_id)
        if state.mode != "answer" or state.question_id != question_id:
            return "Этот вопрос уже не текущий."
        await self._ask_next(chat_id, telegram_id, state)
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
            await self._edit(
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
        await self._edit(pending.chat_id, pending.message_id, text, markup)

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

    async def forget(self, chat_id: int, telegram_id: int) -> None:
        await self._send(chat_id, render.FORGET_CONFIRM, render.forget_keyboard())

    async def forget_confirmed(
        self, chat_id: int, message_id: int, telegram_id: int, confirm: bool
    ) -> None:
        """`/forget` для голоса (006 §15 п. 4): аудио из хранилища и расшифровки из БД."""
        if not confirm:
            await self._edit(chat_id, message_id, render.FORGET_CANCELLED)
            return
        try:
            state = await self.user(telegram_id)
            assert state.user_id is not None
            result = await self.api.forget_audio(state.user_id)
        except ApiError as exc:
            await self._edit(chat_id, message_id, render.api_error_text(exc))
            return
        log.info(
            "audio forgotten", extra={"telegram_id": telegram_id, "audio": result.audio_deleted}
        )
        await self._edit(
            chat_id, message_id, render.forgotten(result.audio_deleted, result.transcripts_deleted)
        )
