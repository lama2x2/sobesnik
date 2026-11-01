"""Голосовой ответ (004 §8): приём аудио, транскрипты, удаление аудио по TTL и по запросу."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sobesnik_api.db.models import Answer, Audio, Transcript, User
from sobesnik_api.errors import (
    STORAGE_UNAVAILABLE,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    UnprocessableError,
)
from sobesnik_api.queue import TaskQueue
from sobesnik_api.services.answers import check_can_answer
from sobesnik_asr import AudioDecodeError, AudioInfo, AudioStorage, StorageError, probe_audio
from sobesnik_core.limits import ANSWER_MAX_CHARS

log = logging.getLogger(__name__)

AUDIO_MIN_SECONDS = 1.0
TranscriptKind = str
"""`asr` или `reference`."""


@dataclass(frozen=True)
class AudioLimits:
    max_bytes: int
    max_seconds: int
    ttl_days: int


def audio_key(answer_id: uuid.UUID) -> str:
    """Ключ объекта: без имён файлов и пользователей."""
    return f"audio/{answer_id}"


def check_audio(data: bytes, limits: AudioLimits) -> AudioInfo:
    if len(data) > limits.max_bytes:
        raise PayloadTooLargeError(
            "audio_too_large", f"файл больше {limits.max_bytes // (1024 * 1024)} МБ"
        )
    try:
        info = probe_audio(data)
    except AudioDecodeError as exc:
        raise UnprocessableError(
            "audio_unsupported", "нужен аудиофайл: ogg/opus, webm, mp3, wav или m4a"
        ) from exc
    if info.duration_s < AUDIO_MIN_SECONDS:
        raise UnprocessableError("audio_too_short", "запись короче секунды")
    if info.duration_s > limits.max_seconds:
        raise UnprocessableError(
            "audio_too_long", f"запись длиннее {limits.max_seconds // 60} минут"
        )
    return info


async def submit_voice_answer(
    db: AsyncSession,
    queue: TaskQueue,
    storage: AudioStorage,
    session_id: uuid.UUID,
    question_id: uuid.UUID,
    data: bytes,
    limits: AudioLimits,
) -> Answer:
    """Проверяет и сохраняет аудио, ставит распознавание в очередь (004 §8.1).

    Хранилище недоступно — `503`, в БД ничего не пишется. Недоступная очередь не мешает приёму.
    """
    session = await check_can_answer(db, session_id, question_id)
    info = check_audio(data, limits)

    answer = Answer(
        id=uuid.uuid4(),
        question_id=question_id,
        session_id=session_id,
        user_id=session.user_id,
        source="user",
        kind="voice",
        text=None,
        status="received",
    )
    key = audio_key(answer.id)
    try:
        await storage.put(key, data, info.mime)
    except StorageError as exc:
        log.warning("audio upload failed", extra={"error": type(exc).__name__})
        raise ServiceUnavailableError("storage_unavailable", STORAGE_UNAVAILABLE) from exc
    audio = Audio(
        answer_id=answer.id,
        s3_key=key,
        mime=info.mime,
        size_bytes=len(data),
        duration_s=info.duration_s,
        expires_at=datetime.now(UTC) + timedelta(days=limits.ttl_days),
    )
    db.add_all([answer, audio])
    try:
        await db.commit()
    except Exception:
        await _delete_quietly(storage, key)
        raise
    try:
        await queue.enqueue_transcription(answer.id)
    except Exception as exc:  # Redis недоступен — ответ уже сохранён
        log.warning(
            "enqueue failed", extra={"answer_id": str(answer.id), "error": type(exc).__name__}
        )
    log.info(
        "voice answer received",
        extra={
            "answer_id": str(answer.id),
            "format": info.format,
            "size_bytes": len(data),
            "duration_s": info.duration_s,
        },
    )
    return answer


async def _delete_quietly(storage: AudioStorage, key: str) -> None:
    try:
        await storage.delete(key)
    except StorageError:
        log.warning("orphan audio left in storage", extra={"s3_key": key})


async def answer_audio(db: AsyncSession, answer_id: uuid.UUID) -> Audio | None:
    result = await db.scalars(select(Audio).where(Audio.answer_id == answer_id))
    return result.first()


async def latest_transcript(
    db: AsyncSession, answer_id: uuid.UUID, kind: TranscriptKind = "asr"
) -> Transcript | None:
    """Последний транскрипт вида `kind` у аудио ответа. По `asr` оценивается голосовой ответ."""
    result = await db.scalars(
        select(Transcript)
        .join(Audio, Audio.id == Transcript.audio_id)
        .where(Audio.answer_id == answer_id, Transcript.kind == kind)
        .order_by(Transcript.created_at.desc(), Transcript.id)
        .limit(1)
    )
    return result.first()


async def add_reference_transcript(
    db: AsyncSession, answer_id: uuid.UUID, text: str, language: str = "ru"
) -> Transcript:
    """Ручная расшифровка (`reference`) к аудио голосового ответа (004 §9.2)."""
    answer = await db.get(Answer, answer_id)
    if answer is None:
        raise NotFoundError("answer_not_found", "ответ не найден")
    audio = await answer_audio(db, answer_id)
    if answer.kind != "voice" or audio is None:
        raise ConflictError("not_voice_answer", "это не голосовой ответ: расшифровывать нечего")
    text = text.strip()
    if not 1 <= len(text) <= ANSWER_MAX_CHARS:
        raise UnprocessableError(
            "bad_transcript", f"расшифровка — от 1 до {ANSWER_MAX_CHARS} символов"
        )
    transcript = Transcript(audio_id=audio.id, kind="reference", text=text, language=language)
    db.add(transcript)
    await db.commit()
    log.info("reference transcript added", extra={"answer_id": str(answer_id)})
    return transcript


@dataclass(frozen=True)
class ForgetResult:
    audio_deleted: int
    transcripts_deleted: int


async def forget_audio(db: AsyncSession, storage: AudioStorage, user_id: uuid.UUID) -> ForgetResult:
    """Удаляет аудио пользователя из хранилища, затем строки `audio` и транскрипты (004 §8.5).

    Если хранилище недоступно — `503`, а в БД ничего не удаляется: объект не должен остаться
    без строки, по которой его можно найти. Повторный вызов безопасен.
    """
    if await db.get(User, user_id) is None:
        raise NotFoundError("user_not_found", "пользователь не найден")
    audios = list(
        await db.scalars(
            select(Audio)
            .join(Answer, Answer.id == Audio.answer_id)
            .where(Answer.user_id == user_id)
        )
    )
    try:
        for audio in audios:
            if audio.deleted_at is None:
                await storage.delete(audio.s3_key)
    except StorageError as exc:
        log.warning("forget failed", extra={"user_id": str(user_id)})
        raise ServiceUnavailableError("storage_unavailable", STORAGE_UNAVAILABLE) from exc
    ids = [a.id for a in audios]
    transcripts = 0
    if ids:
        transcripts = len(
            list(await db.scalars(select(Transcript.id).where(Transcript.audio_id.in_(ids))))
        )
        await db.execute(delete(Audio).where(Audio.id.in_(ids)))  # транскрипты — каскадом
    await db.commit()
    log.info(
        "user audio forgotten",
        extra={"user_id": str(user_id), "audio": len(ids), "transcripts": transcripts},
    )
    return ForgetResult(audio_deleted=len(ids), transcripts_deleted=transcripts)


async def cleanup_expired_audio(
    db: AsyncSession, storage: AudioStorage, now: datetime | None = None
) -> int:
    """Удаляет из хранилища аудио с истёкшим TTL и помечает строки (ASR-5).

    Хранилище недоступно — остаток ждёт следующего запуска.
    """
    now = now or datetime.now(UTC)
    expired = list(
        await db.scalars(select(Audio).where(Audio.deleted_at.is_(None), Audio.expires_at < now))
    )
    deleted = 0
    for audio in expired:
        try:
            await storage.delete(audio.s3_key)
        except StorageError:
            log.warning("audio cleanup interrupted", extra={"left": len(expired) - deleted})
            break
        audio.deleted_at = now
        deleted += 1
    await db.commit()
    if deleted:
        log.info("expired audio deleted", extra={"audio": deleted})
    return deleted
