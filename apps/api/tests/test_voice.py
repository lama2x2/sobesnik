"""Голосовой ответ: приём, распознавание, оценка транскрипта, прогоны по виду транскрипта,
CSV, TTL и удаление аудио (004)."""

import csv
import io
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arq import Retry
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.db.models import Audio, Evaluation, Transcript
from sobesnik_api.errors import AppError
from sobesnik_api.export import COMMON, TAIL, ExportFilter, select_rows, write_csv
from sobesnik_api.services.eval_runs import RunFilter, create_run
from sobesnik_api.services.voice import AudioLimits, add_reference_transcript, check_audio
from sobesnik_api.worker import (
    WorkerDeps,
    process_answer,
    process_cleanup,
    process_run,
    process_transcription,
    recover,
)
from sobesnik_asr import (
    ASRError,
    AudioDecodeError,
    FakeTranscriber,
    MemoryStorage,
    StorageUnavailableError,
    probe_audio,
)
from sobesnik_asr.samples import mp4_video_bytes, ogg_opus_bytes, wav_bytes
from sobesnik_llm import FakeProvider

SPOKEN = "Составной индекс упорядочен по первому столбцу."
LIMITS = AudioLimits(max_bytes=1024 * 1024, max_seconds=300, ttl_days=30)


def voice_url(session: dict[str, Any], position: int = 1) -> str:
    qid = session["questions"][position - 1]["id"]
    return f"/sessions/{session['id']}/questions/{qid}/answer/voice"


async def send_voice(
    client: AsyncClient,
    session: dict[str, Any],
    position: int = 1,
    data: bytes | None = None,
) -> Any:
    files = {"file": ("голос.ogg", data if data is not None else ogg_opus_bytes(2.0), "audio/ogg")}
    return await client.post(voice_url(session, position), files=files)


async def submit_voice(
    client: AsyncClient, session: dict[str, Any], position: int = 1
) -> uuid.UUID:
    response = await send_voice(client, session, position)
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["answer_id"])


def eval_with_doubts(replies: Any, doubts: list[str]) -> str:
    data = json.loads(replies.evaluation([3, 2, 2, 2, 2]))
    data["asr_doubts"] = doubts
    return json.dumps(data, ensure_ascii=False)


async def answer_status(engine: AsyncEngine, answer_id: uuid.UUID) -> tuple[str, str | None]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, error FROM answer WHERE id = :id"), {"id": answer_id}
            )
        ).one()
    return row.status, row.error


async def voice_done(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    position: int = 1,
) -> uuid.UUID:
    """Голосовой ответ, распознанный и оценённый."""
    answer_id = await submit_voice(client, session, position)
    llm.replies = [replies.evaluation()]
    await process_transcription(deps, answer_id)
    await process_answer(deps, answer_id)
    return answer_id


# --- приём


async def test_submit_voice(
    client: AsyncClient,
    session: dict[str, Any],
    queue: Any,
    storage: MemoryStorage,
    engine: AsyncEngine,
) -> None:
    data = ogg_opus_bytes(2.0)
    response = await send_voice(client, session, data=data)

    assert response.status_code == 202, response.text
    body = response.json()
    answer_id = uuid.UUID(body["answer_id"])
    assert body["status"] == "received"
    assert queue.transcriptions == [answer_id]
    assert queue.evaluations == []
    assert storage.objects[f"audio/{answer_id}"] == (data, "audio/ogg")
    async with engine.connect() as conn:
        answer = (
            await conn.execute(
                text("SELECT kind, text, status FROM answer WHERE id = :id"), {"id": answer_id}
            )
        ).one()
        audio = (
            await conn.execute(text("SELECT * FROM audio WHERE answer_id = :id"), {"id": answer_id})
        ).one()
    assert (answer.kind, answer.text, answer.status) == ("voice", None, "received")
    assert audio.s3_key == f"audio/{answer_id}"
    assert audio.mime == "audio/ogg"
    assert audio.size_bytes == len(data)
    assert audio.duration_s == pytest.approx(2.0, abs=0.1)
    assert audio.expires_at - audio.created_at == pytest.approx(
        timedelta(days=30), abs=timedelta(minutes=1)
    )
    assert audio.deleted_at is None


async def test_submit_video_note_keeps_audio_only(
    client: AsyncClient, session: dict[str, Any], storage: MemoryStorage
) -> None:
    """Кружочек: в хранилище только звук, лица нет (006 §4.6)."""
    response = await send_voice(client, session, data=mp4_video_bytes(2.0))
    assert response.status_code == 202, response.text
    data, mime = storage.objects[f"audio/{response.json()['answer_id']}"]
    assert mime == "audio/mp4"
    assert not probe_audio(data).has_video


async def test_submit_wav(client: AsyncClient, session: dict[str, Any]) -> None:
    response = await send_voice(client, session, data=wav_bytes(1.5))
    assert response.status_code == 202


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"not an audio file at all" * 10, "audio_unsupported"),
        (wav_bytes(0.5), "audio_too_short"),
    ],
)
async def test_voice_rejected(
    client: AsyncClient, session: dict[str, Any], storage: MemoryStorage, data: bytes, code: str
) -> None:
    response = await send_voice(client, session, data=data)
    assert (response.status_code, response.json()["error"]["code"]) == (422, code)
    assert storage.objects == {}


def test_check_audio_limits() -> None:
    with pytest.raises(AppError) as too_large:
        check_audio(b"x" * 101, AudioLimits(max_bytes=100, max_seconds=300, ttl_days=30))
    assert (too_large.value.status_code, too_large.value.code) == (413, "audio_too_large")
    with pytest.raises(AppError) as too_long:
        check_audio(wav_bytes(3.0), AudioLimits(max_bytes=10**6, max_seconds=2, ttl_days=30))
    assert too_long.value.code == "audio_too_long"
    assert check_audio(wav_bytes(1.5), LIMITS).format == "wav"


async def test_voice_session_errors(client: AsyncClient, session: dict[str, Any]) -> None:
    await submit_voice(client, session)
    again = await send_voice(client, session)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_answered")
    qid = session["questions"][0]["id"]
    missing = await client.post(
        f"/sessions/{uuid.uuid4()}/questions/{qid}/answer/voice",
        files={"file": ("a.ogg", ogg_opus_bytes(), "audio/ogg")},
    )
    assert missing.json()["error"]["code"] == "session_not_found"


async def test_storage_unavailable(
    client: AsyncClient, session: dict[str, Any], storage: MemoryStorage, engine: AsyncEngine
) -> None:
    storage.available = False
    response = await send_voice(client, session)
    assert (response.status_code, response.json()["error"]["code"]) == (
        503,
        "storage_unavailable",
    )
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM answer")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM audio")) == 0


async def test_redis_unavailable_still_accepts(
    client: AsyncClient, session: dict[str, Any], queue: Any
) -> None:
    queue.broken = True
    response = await send_voice(client, session)
    assert response.status_code == 202


# --- распознавание и оценка


async def test_transcribe_and_evaluate(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    queue: Any,
    transcriber: FakeTranscriber,
    replies: Any,
    engine: AsyncEngine,
) -> None:
    answer_id = await submit_voice(client, session)
    await process_transcription(deps, answer_id)

    assert await answer_status(engine, answer_id) == ("evaluating", None)
    assert queue.evaluations == [answer_id]
    assert transcriber.calls[0][1] == "ru"
    async with deps.sessionmaker() as db:
        transcript = (await db.scalars(select(Transcript))).one()
    assert (transcript.kind, transcript.text, transcript.language) == ("asr", SPOKEN, "ru")
    assert transcript.asr_model == "small"
    assert transcript.latency_ms == 5
    assert transcript.avg_logprob == -0.2
    assert transcript.params == {"language": "ru"}

    doubts = ["распознано «кэш» — возможно, сказано «кеш»"]
    llm.replies = [eval_with_doubts(replies, doubts)]
    await process_answer(deps, answer_id)

    assert SPOKEN in llm.requests[-1].prompt
    assert "автоматическая расшифровка" in llm.requests[-1].prompt
    assert "asr_doubts" in (llm.requests[-1].json_schema or {})["properties"]
    assert await answer_status(engine, answer_id) == ("done", None)
    async with deps.sessionmaker() as db:
        evaluation = (await db.scalars(select(Evaluation))).one()
    assert evaluation.transcript_id == transcript.id
    assert evaluation.feedback is not None
    assert evaluation.feedback["asr_doubts"] == doubts

    body = (await client.get(f"/answers/{answer_id}")).json()
    assert body["kind"] == "voice"
    assert body["text"] is None
    assert body["transcript"]["text"] == SPOKEN
    assert body["transcript"]["kind"] == "asr"
    assert body["audio"]["mime"] == "audio/ogg"
    assert body["audio"]["deleted"] is False
    assert body["evaluation"]["asr_doubts"] == doubts
    session_body = (await client.get(f"/sessions/{session['id']}")).json()
    assert session_body["questions"][0]["answer"]["kind"] == "voice"


async def test_text_answer_has_no_asr_doubts(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, llm: FakeProvider, replies: Any
) -> None:
    response = await client.post(
        f"/sessions/{session['id']}/questions/{session['questions'][0]['id']}/answer",
        json={"text": "Ответ текстом"},
    )
    answer_id = uuid.UUID(response.json()["answer_id"])
    llm.replies = [eval_with_doubts(replies, ["выдумано"])]
    await process_answer(deps, answer_id)

    assert "asr_doubts" not in (llm.requests[-1].json_schema or {})["properties"]
    body = (await client.get(f"/answers/{answer_id}")).json()
    assert body["evaluation"]["asr_doubts"] == []
    assert body["transcript"] is None and body["audio"] is None


async def test_transcription_repeat_does_not_duplicate(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, queue: Any, engine: AsyncEngine
) -> None:
    answer_id = await submit_voice(client, session)
    await process_transcription(deps, answer_id)
    # повторная постановка, пока ответ ещё не оценён: транскрипт не дублируется
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE answer SET status = 'transcribing' WHERE id = :id"), {"id": answer_id}
        )
    await process_transcription(deps, answer_id)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM transcript")) == 1
    assert queue.evaluations == [answer_id, answer_id]


async def test_silence_is_asr_empty(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    transcriber: FakeTranscriber,
    queue: Any,
    engine: AsyncEngine,
) -> None:
    transcriber.text = "   "
    answer_id = await submit_voice(client, session)
    await process_transcription(deps, answer_id)
    assert await answer_status(engine, answer_id) == ("failed", "asr_empty")
    assert queue.evaluations == []
    # после сбоя можно ответить заново
    assert (await send_voice(client, session)).status_code == 202


async def test_unreadable_audio_no_retry(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    transcriber: FakeTranscriber,
    engine: AsyncEngine,
) -> None:
    transcriber.error = AudioDecodeError("битый файл")
    answer_id = await submit_voice(client, session)
    await process_transcription(deps, answer_id, job_try=1)
    assert await answer_status(engine, answer_id) == ("failed", "audio_unreadable")


@pytest.mark.parametrize("error", [ASRError("модель не скачалась"), StorageUnavailableError("s3")])
async def test_asr_failure_retries(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    transcriber: FakeTranscriber,
    engine: AsyncEngine,
    error: Exception,
) -> None:
    transcriber.error = error
    answer_id = await submit_voice(client, session)
    with pytest.raises(Retry):
        await process_transcription(deps, answer_id, job_try=1)
    assert await answer_status(engine, answer_id) == ("transcribing", None)
    await process_transcription(deps, answer_id, job_try=deps.settings.asr_max_tries)
    assert await answer_status(engine, answer_id) == ("failed", "asr_failed")


async def test_audio_missing(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    storage: MemoryStorage,
    engine: AsyncEngine,
) -> None:
    answer_id = await submit_voice(client, session)
    storage.objects.clear()
    await process_transcription(deps, answer_id)
    assert await answer_status(engine, answer_id) == ("failed", "audio_missing")


async def test_voice_without_transcript_fails_evaluation(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, engine: AsyncEngine
) -> None:
    answer_id = await submit_voice(client, session)
    await process_answer(deps, answer_id)
    assert await answer_status(engine, answer_id) == ("failed", "no_transcript")


async def test_logs_have_no_transcript_or_audio(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await voice_done(client, session, deps, llm, replies)
    assert SPOKEN not in caplog.text
    assert "голос.ogg" not in caplog.text
    assert "answer transcribed" in caplog.text


async def test_recover_routes_voice_answers(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    queue: Any,
    engine: AsyncEngine,
) -> None:
    received = await submit_voice(client, session, 1)
    transcribed = await submit_voice(client, session, 2)
    await process_transcription(deps, transcribed)  # → evaluating
    queue.transcriptions.clear()
    queue.evaluations.clear()

    await recover(deps, queue)
    assert queue.transcriptions == [received]
    assert queue.evaluations == [transcribed]


# --- прогоны, расшифровки, CSV


async def test_reference_transcript_and_runs(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
) -> None:
    voice_id = await voice_done(client, session, deps, llm, replies, 1)
    response = await client.post(
        f"/sessions/{session['id']}/questions/{session['questions'][1]['id']}/answer",
        json={"text": "Ответ текстом"},
    )
    text_id = uuid.UUID(response.json()["answer_id"])
    await process_answer(deps, text_id)

    async with deps.sessionmaker() as db:
        reference = await add_reference_transcript(db, voice_id, "  Эталонная расшифровка  ")
        with pytest.raises(AppError) as not_voice:
            await add_reference_transcript(db, text_id, "текст")
        assert not_voice.value.code == "not_voice_answer"
        with pytest.raises(AppError):
            await add_reference_transcript(db, voice_id, "   ")
    assert reference.text == "Эталонная расшифровка"

    async def run_with(kind: str | None) -> list[Evaluation]:
        async with deps.sessionmaker() as db:
            run = await create_run(
                db,
                name=f"run-{kind}",
                provider="fake",
                model=llm.model,
                run_filter=RunFilter(session_id=uuid.UUID(session["id"])),
                transcript_kind=kind,
            )
        llm.requests.clear()
        await process_run(deps, run.id)
        async with deps.sessionmaker() as db:
            return list(
                await db.scalars(select(Evaluation).where(Evaluation.eval_run_id == run.id))
            )

    live = await run_with(None)
    assert {e.answer_id for e in live} == {voice_id, text_id}
    asr = await run_with("asr")
    assert [e.answer_id for e in asr] == [voice_id]
    assert asr[0].transcript_id is not None and asr[0].transcript_id != reference.id
    ref = await run_with("reference")
    assert [e.answer_id for e in ref] == [voice_id]
    assert ref[0].transcript_id == reference.id
    assert "Эталонная расшифровка" in llm.requests[-1].prompt
    # ручная расшифровка — не распознавание: сомнений в распознавании у неё не бывает
    assert "письменный ответ" in llm.requests[-1].prompt

    # «боевая» оценка и транскрипт в API не изменились
    body = (await client.get(f"/answers/{voice_id}")).json()
    assert body["transcript"]["kind"] == "asr"

    async with deps.sessionmaker() as db:
        rows = await select_rows(
            db, ExportFilter(live=True, run_ids=[ref[0].eval_run_id or uuid.uuid4()])
        )
    out = io.StringIO()
    write_csv(out, rows)
    table = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert set(COMMON) <= set(table[0]) and set(TAIL) <= set(table[0])
    by_kind = {(r["answer_kind"], r["transcript_kind"]): r for r in table}
    assert by_kind[("voice", "reference")]["answer"] == "Эталонная расшифровка"
    assert by_kind[("voice", "asr")]["answer"] == SPOKEN
    assert by_kind[("voice", "asr")]["asr_model"] == "small"
    assert float(by_kind[("voice", "asr")]["audio_duration_s"]) == pytest.approx(2.0, abs=0.1)
    assert by_kind[("text", "")]["answer"] == "Ответ текстом"
    assert by_kind[("text", "")]["transcript_id"] == ""


async def test_cli_transcript_and_run_flag(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    queue: Any,
    tmp_path: Any,
) -> None:
    from sobesnik_api.cli import build_parser, run_command

    voice_id = await voice_done(client, session, deps, llm, replies)
    ref_file = tmp_path / "ref.txt"
    ref_file.write_text("Ручная расшифровка", encoding="utf-8")
    parser = build_parser()
    async with deps.sessionmaker() as db:
        args = parser.parse_args(["transcript", "add", str(voice_id), "--file", str(ref_file)])
        assert await run_command(args, deps.settings, db, queue) == 0
        args = parser.parse_args(["eval-run", "create", "--name", "r", "--transcript", "reference"])
        assert await run_command(args, deps.settings, db, queue) == 0
    async with deps.sessionmaker() as db:
        kinds = list(await db.scalars(select(Transcript.kind).order_by(Transcript.created_at)))
        run_kinds = list(await db.scalars(text("SELECT transcript_kind FROM eval_run")))
    assert kinds == ["asr", "reference"]
    assert run_kinds == ["reference"]


# --- TTL и удаление


async def test_cleanup_expired_audio(
    client: AsyncClient,
    session: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    storage: MemoryStorage,
    engine: AsyncEngine,
) -> None:
    old = await voice_done(client, session, deps, llm, replies, 1)
    fresh = await submit_voice(client, session, 2)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE audio SET expires_at = now() - interval '1 day' WHERE answer_id = :id"),
            {"id": old},
        )

    storage.available = False
    assert await process_cleanup(deps) == 0  # хранилище недоступно — ждём следующего запуска
    storage.available = True
    assert await process_cleanup(deps) == 1
    assert await process_cleanup(deps) == 0

    assert f"audio/{old}" not in storage.objects
    assert f"audio/{fresh}" in storage.objects
    async with deps.sessionmaker() as db:
        audio = (await db.scalars(select(Audio).where(Audio.answer_id == old))).one()
        transcripts = list(await db.scalars(select(Transcript)))
    assert audio.deleted_at is not None
    assert len(transcripts) == 1  # TTL не касается транскриптов
    body = (await client.get(f"/answers/{old}")).json()
    assert body["audio"]["deleted"] is True
    assert body["transcript"]["text"] == SPOKEN


async def test_forget_audio(
    client: AsyncClient,
    session: dict[str, Any],
    user: dict[str, Any],
    deps: WorkerDeps,
    llm: FakeProvider,
    replies: Any,
    storage: MemoryStorage,
    engine: AsyncEngine,
) -> None:
    voice_id = await voice_done(client, session, deps, llm, replies)
    async with deps.sessionmaker() as db:
        await add_reference_transcript(db, voice_id, "Ручная")
    url = f"/users/{user['id']}/data?scope=audio"

    storage.available = False
    unavailable = await client.delete(url)
    assert unavailable.status_code == 503
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM transcript")) == 2

    storage.available = True
    response = await client.delete(url)
    assert response.status_code == 200, response.text
    assert response.json() == {"audio_deleted": 1, "transcripts_deleted": 2, "reviews_deleted": 0}
    assert storage.objects == {}
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM audio")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM transcript")) == 0
        evaluation = (
            await conn.execute(text("SELECT status, transcript_id FROM evaluation"))
        ).one()
    assert evaluation.status == "done" and evaluation.transcript_id is None  # оценка осталась
    body = (await client.get(f"/answers/{voice_id}")).json()
    assert body["transcript"] is None and body["audio"] is None
    assert body["evaluation"] is not None

    again = await client.delete(url)
    assert again.json() == {"audio_deleted": 0, "transcripts_deleted": 0, "reviews_deleted": 0}
    assert (await client.delete(f"/users/{uuid.uuid4()}/data?scope=audio")).status_code == 404
    assert (await client.delete(url.replace("audio", "video"))).status_code == 422


async def test_expired_audio_cannot_be_transcribed(
    client: AsyncClient, session: dict[str, Any], deps: WorkerDeps, engine: AsyncEngine
) -> None:
    answer_id = await submit_voice(client, session)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE audio SET deleted_at = :now WHERE answer_id = :id"),
            {"now": datetime.now(UTC), "id": answer_id},
        )
    await process_transcription(deps, answer_id)
    assert await answer_status(engine, answer_id) == ("failed", "audio_missing")
