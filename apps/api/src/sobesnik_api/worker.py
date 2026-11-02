"""Воркер arq: распознавание и оценка ответов, прогоны, очистка аудио (003 §8.2–8.3, §9.1, 004 §8).

    arq sobesnik_api.worker.WorkerSettings

Логика задач — в функциях `process_transcription`, `process_answer`, `process_run` и
`process_cleanup`: тесты вызывают их напрямую с `WorkerDeps`, без Redis.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from arq import Retry
from arq.cron import cron
from arq.worker import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sobesnik_api.db.models import Answer, EvalRun, Transcript
from sobesnik_api.db.session import make_engine, make_sessionmaker
from sobesnik_api.events import AnswerEvents, NullAnswerEvents, RedisAnswerEvents
from sobesnik_api.logging_setup import configure_logging
from sobesnik_api.queue import (
    EVALUATE_JOB,
    RUN_JOB,
    TRANSCRIBE_JOB,
    ArqQueue,
    TaskQueue,
    redis_settings,
)
from sobesnik_api.services.answers import IN_PROGRESS
from sobesnik_api.services.eval_runs import execute_run
from sobesnik_api.services.evaluation import EvalConfig, evaluate_and_store
from sobesnik_api.services.voice import answer_audio, cleanup_expired_audio, latest_transcript
from sobesnik_api.settings import Settings
from sobesnik_asr import (
    ASRError,
    AudioDecodeError,
    AudioNotFoundError,
    AudioStorage,
    StorageError,
    Transcriber,
    make_transcriber,
)
from sobesnik_llm import LLMProvider, LLMUnavailableError, make_provider

log = logging.getLogger(__name__)

RETRY_DELAYS_S = (10, 30, 60, 120)
"""Паузы между попытками, если LLM или распознавание недоступны: около 4 минут на 5 попыток."""
EVALUATE_TIMEOUT_S = 15 * 60
TRANSCRIBE_TIMEOUT_S = 15 * 60
RUN_TIMEOUT_S = 24 * 60 * 60

RunLLM = Callable[[EvalRun], AbstractAsyncContextManager[LLMProvider]]


@dataclass
class WorkerDeps:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    llm: LLMProvider
    """Модель роли eval — для «боевых» оценок."""
    run_llm: RunLLM
    """Провайдер с моделью прогона."""
    transcriber: Transcriber
    storage: AudioStorage
    queue: TaskQueue
    """Чтобы после распознавания поставить оценку."""
    events: AnswerEvents = field(default_factory=NullAnswerEvents)
    """События смены статуса ответа для бота (006 §6)."""


def retry_delay(job_try: int) -> int:
    return RETRY_DELAYS_S[min(job_try, len(RETRY_DELAYS_S)) - 1]


async def _set_status(
    deps: WorkerDeps, db: AsyncSession, answer: Answer, status: str, error: str | None = None
) -> None:
    """Меняет статус, коммитит и после коммита публикует событие, если статус изменился."""
    changed = (answer.status, answer.error) != (status, error)
    answer.status, answer.error = status, error
    await db.commit()
    if changed:
        await deps.events.publish(answer)


async def _fail(deps: WorkerDeps, db: AsyncSession, answer: Answer, error: str) -> None:
    await _set_status(deps, db, answer, "failed", error)
    log.warning("answer failed", extra={"answer_id": str(answer.id), "error": error})


async def _enqueue_evaluation(deps: WorkerDeps, answer_id: uuid.UUID) -> None:
    try:
        await deps.queue.enqueue_evaluation(answer_id)
    except Exception as exc:  # ответ в evaluating: его подхватит воркер при старте
        log.warning(
            "enqueue failed", extra={"answer_id": str(answer_id), "error": type(exc).__name__}
        )


def _after_transcription(answer: Answer) -> str:
    return "imported" if answer.source == "dataset" else "evaluating"


async def process_transcription(deps: WorkerDeps, answer_id: uuid.UUID, job_try: int = 1) -> None:
    """Распознаёт голосовой ответ и ставит его оценку в очередь (004 §8.2).

    Ответ из датасета после распознавания получает `imported`: его оценивают только прогоны
    (005 §7.1). Сбой распознавания или хранилища — `Retry`; на последней попытке ответ получает
    `failed`.
    """
    settings = deps.settings
    async with deps.sessionmaker() as db:
        answer = await db.get(Answer, answer_id)
        if answer is None or answer.kind != "voice":
            return
        if answer.status not in ("received", "transcribing"):
            return
        if await latest_transcript(db, answer.id) is not None:  # повторная постановка
            await _set_status(deps, db, answer, _after_transcription(answer))
            if answer.status == "evaluating":
                await _enqueue_evaluation(deps, answer.id)
            return
        audio = await answer_audio(db, answer.id)
        if audio is None or audio.deleted_at is not None:
            await _fail(deps, db, answer, "audio_missing")
            return
        await _set_status(deps, db, answer, "transcribing")

        try:
            data = await deps.storage.get(audio.s3_key)
            result = await deps.transcriber.transcribe(data, language=settings.asr_language_code)
        except AudioNotFoundError:
            await _fail(deps, db, answer, "audio_missing")
            return
        except AudioDecodeError:
            await _fail(deps, db, answer, "audio_unreadable")
            return
        except (ASRError, StorageError) as exc:
            if job_try >= settings.asr_max_tries:
                await _fail(deps, db, answer, "asr_failed")
                return
            log.info(
                "asr failed, retry",
                extra={
                    "answer_id": str(answer_id),
                    "job_try": job_try,
                    "error": type(exc).__name__,
                },
            )
            raise Retry(defer=retry_delay(job_try)) from None

        text = result.text.strip()
        db.add(
            Transcript(
                audio_id=audio.id,
                kind="asr",
                text=text,
                language=result.language,
                language_prob=result.language_probability,
                asr_model=result.model,
                avg_logprob=result.avg_logprob,
                latency_ms=result.latency_ms,
                params=result.params,
            )
        )
        log.info(
            "answer transcribed",
            extra={
                "answer_id": str(answer_id),
                "asr_model": result.model,
                "language": result.language,
                "duration_s": result.duration_s,
                "latency_ms": result.latency_ms,
                "chars": len(text),
            },
        )
        if not text:  # тишина: бот попросит повторить или ответить текстом
            await _fail(deps, db, answer, "asr_empty")
            return
        await _set_status(deps, db, answer, _after_transcription(answer))
    if answer.status == "evaluating":
        await _enqueue_evaluation(deps, answer_id)


async def process_answer(deps: WorkerDeps, answer_id: uuid.UUID, job_try: int = 1) -> None:
    """Оценивает ответ: текст или транскрипт голосового. Повторный запуск для уже оценённого
    ответа ничего не делает.

    Если LLM недоступна, бросает `Retry`; на последней попытке ответ получает `failed`.
    """
    settings = deps.settings
    async with deps.sessionmaker() as db:
        answer = await db.get(Answer, answer_id)
        if answer is None or answer.status not in IN_PROGRESS:
            return
        transcript = None
        if answer.kind == "voice":
            transcript = await latest_transcript(db, answer.id)
            if transcript is None:
                await _fail(deps, db, answer, "no_transcript")
                return
        await _set_status(deps, db, answer, "evaluating")

        config = EvalConfig.latest(
            max_retries=settings.llm_max_retries, num_ctx=settings.llm_num_ctx
        )
        try:
            evaluation = await evaluate_and_store(
                db, deps.llm, config, answer, transcript=transcript
            )
        except LLMUnavailableError:
            if job_try >= settings.eval_max_tries:
                await _set_status(deps, db, answer, "failed", "llm_unavailable")
                log.warning("answer failed", extra={"answer_id": str(answer_id), "tries": job_try})
                return
            log.info(
                "llm unavailable, retry",
                extra={"answer_id": str(answer_id), "job_try": job_try},
            )
            raise Retry(defer=retry_delay(job_try)) from None
        status = "done" if evaluation.status == "done" else "failed"
        await _set_status(deps, db, answer, status, evaluation.error)


async def process_run(deps: WorkerDeps, run_id: uuid.UUID) -> None:
    settings = deps.settings
    await execute_run(
        deps.sessionmaker,
        run_id,
        deps.run_llm,
        max_retries=settings.llm_max_retries,
        num_ctx=settings.llm_num_ctx,
    )


async def process_cleanup(deps: WorkerDeps) -> int:
    async with deps.sessionmaker() as db:
        return await cleanup_expired_audio(db, deps.storage)


async def recover(deps: WorkerDeps, queue: TaskQueue) -> None:
    """Ставит в очередь то, что зависло после перезапуска: ответы и прогоны в работе.

    Голосовые ответы до распознавания — на распознавание, остальные — на оценку.
    """
    async with deps.sessionmaker() as db:
        answers = list(
            await db.execute(
                select(Answer.id, Answer.kind, Answer.status).where(Answer.status.in_(IN_PROGRESS))
            )
        )
        runs = list(
            await db.scalars(select(EvalRun.id).where(EvalRun.status.in_(("queued", "running"))))
        )
    for answer_id, kind, status in answers:
        if kind == "voice" and status in ("received", "transcribing"):
            await queue.enqueue_transcription(answer_id)
        else:
            await queue.enqueue_evaluation(answer_id)
    for run_id in runs:
        await queue.enqueue_run(run_id)
    if answers or runs:
        log.info("recovered jobs", extra={"answers": len(answers), "runs": len(runs)})


def make_run_llm(settings: Settings, shared: LLMProvider) -> RunLLM:
    """Провайдер роли eval с моделью прогона. Если модель та же — общий провайдер."""

    @asynccontextmanager
    async def run_llm(run: EvalRun) -> AsyncIterator[LLMProvider]:
        if run.model == shared.model:
            yield shared
            return
        config = settings.llm_config("eval").model_copy(update={"model": run.model})
        provider = make_provider(config)
        try:
            yield provider
        finally:
            await provider.aclose()

    return run_llm


async def startup(ctx: dict[str, Any]) -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    llm = make_provider(settings.llm_config("eval"))
    storage = settings.audio_storage()
    queue = ArqQueue(settings.redis_url, pool=ctx["redis"])
    ctx["engine"] = engine
    ctx["deps"] = WorkerDeps(
        settings=settings,
        sessionmaker=make_sessionmaker(engine),
        llm=llm,
        run_llm=make_run_llm(settings, llm),
        transcriber=make_transcriber(settings.asr_config()),
        storage=storage,
        queue=queue,
        events=RedisAnswerEvents.from_url(settings.redis_url, settings.answer_events_stream),
    )
    try:
        await storage.ensure_bucket()
    except StorageError as exc:
        log.warning("audio bucket not ready", extra={"error": type(exc).__name__})
    await recover(ctx["deps"], queue)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["deps"].llm.aclose()
    await ctx["deps"].transcriber.aclose()
    await ctx["deps"].events.aclose()
    await ctx["engine"].dispose()


async def transcribe_answer_job(ctx: dict[str, Any], answer_id: str) -> None:
    await process_transcription(ctx["deps"], uuid.UUID(answer_id), ctx["job_try"])


async def evaluate_answer_job(ctx: dict[str, Any], answer_id: str) -> None:
    await process_answer(ctx["deps"], uuid.UUID(answer_id), ctx["job_try"])


async def run_eval_job(ctx: dict[str, Any], run_id: str) -> None:
    await process_run(ctx["deps"], uuid.UUID(run_id))


async def cleanup_audio_job(ctx: dict[str, Any]) -> None:
    await process_cleanup(ctx["deps"])


_settings = Settings()


class WorkerSettings:
    functions = (
        func(
            transcribe_answer_job,
            name=TRANSCRIBE_JOB,
            timeout=TRANSCRIBE_TIMEOUT_S,
            max_tries=_settings.asr_max_tries,
        ),
        func(
            evaluate_answer_job,
            name=EVALUATE_JOB,
            timeout=EVALUATE_TIMEOUT_S,
            max_tries=_settings.eval_max_tries,
        ),
        func(run_eval_job, name=RUN_JOB, timeout=RUN_TIMEOUT_S, max_tries=3),
    )
    # аудио с истёкшим TTL удаляется раз в час
    cron_jobs = (cron(cleanup_audio_job, minute=7, run_at_startup=True),)
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings(_settings.redis_url)
    max_jobs = _settings.worker_max_jobs
    # Результаты не храним: иначе задачу с тем же job_id нельзя поставить снова (restart прогона).
    keep_result = 0
