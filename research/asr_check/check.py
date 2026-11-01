"""Быстрая живая проверка голосового ответа (004 §12): несколько минут.

    research/asr_check/cut.sh ЗАПИСЬ 00:10:00 answer1      # на хосте: фрагмент 60 с
    docker compose run --rm tools uv run python research/asr_check/check.py --asr-only
    docker compose up -d --build
    docker compose run --rm tools uv run python research/asr_check/check.py
    ollama stop qwen3.5:4b

`--asr-only` — замер скорости: каждый `.ogg` из research/data/asr_check/ распознаётся
моделью whisper прямо в контейнере tools (без API и LLM). Если рядом лежит `.txt`
с расшифровкой, печатается WER.

Без флага — путь через API: вакансия из кэша → сессия → фрагменты отправляются голосом →
воркер распознаёт и оценивает. Затем к каждому ответу добавляется ручная расшифровка (`.txt`,
а если её нет — распознанный текст без правки, 004 §16 п. 4) и делается прогон по `reference`
той же моделью; оценки по `asr` и `reference` стоят рядом в CSV.
"""

import argparse
import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from sobesnik_api.db.session import make_engine, make_sessionmaker
from sobesnik_api.export import ExportFilter, select_rows, write_csv
from sobesnik_api.queue import ArqQueue
from sobesnik_api.services.eval_runs import RunFilter, create_run, get_run
from sobesnik_api.services.voice import add_reference_transcript
from sobesnik_api.settings import Settings
from sobesnik_asr import FasterWhisperTranscriber, probe_audio

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "research" / "data" / "asr_check"
VACANCIES = ROOT / "research" / "data" / "vacancies"
API = "http://api:8000/api/v1"
POLL_S = 2.0
PREVIEW_CHARS = 160


def wer(reference: str, hypothesis: str) -> float:
    """Доля ошибок по словам (расстояние Левенштейна по словам / длина эталона)."""
    ref = reference.lower().split()
    hyp = hypothesis.lower().split()
    row = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, start=1):
        prev, row[0] = row[0], i
        for j, other in enumerate(hyp, start=1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (word != other))
    return row[-1] / max(len(ref), 1)


def preview(text: str) -> str:
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


def samples() -> list[Path]:
    files = sorted(DATA.glob("*.ogg"))
    if not files:
        raise SystemExit(
            f"нет записей в {DATA.relative_to(ROOT)}: нарежьте их research/asr_check/cut.sh"
        )
    return files


async def asr_only(settings: Settings, model: str) -> None:
    asr = FasterWhisperTranscriber(
        model,
        model_dir=settings.asr_model_dir,
        compute_type=settings.asr_compute_type,
        beam_size=settings.asr_beam_size,
        cpu_threads=settings.asr_cpu_threads,
    )
    started = time.perf_counter()
    await asyncio.to_thread(asr._load)  # загрузка (и первое скачивание) — отдельно от скорости
    print(f"модель {model}: загрузка {time.perf_counter() - started:.1f} с")
    for path in samples():
        data = path.read_bytes()
        info = probe_audio(data)
        result = await asr.transcribe(data, language=settings.asr_language_code)
        seconds = result.latency_ms / 1000
        print(
            f"\n{path.name}: {info.duration_s:.0f} с аудио → {seconds:.1f} с распознавания "
            f"({seconds / info.duration_s * 60:.1f} с на минуту речи), язык {result.language}, "
            f"avg_logprob {result.avg_logprob}"
        )
        reference = path.with_suffix(".txt")
        if reference.exists():
            print(f"  WER {wer(reference.read_text(encoding='utf-8'), result.text):.2f}")
        print(f"  текст: {preview(result.text)}")
    await asr.aclose()


async def wait_answer(api: httpx.AsyncClient, answer_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    seen = ""
    started = time.monotonic()
    while True:
        body: dict[str, Any] = (await api.get(f"/answers/{answer_id}")).json()
        if body["status"] != seen:
            seen = body["status"]
            print(f"  {time.monotonic() - started:5.0f} с  {seen}")
        if body["status"] in ("done", "failed") or time.monotonic() > deadline:
            return body
        await asyncio.sleep(POLL_S)


def print_result(body: dict[str, Any]) -> None:
    transcript = body.get("transcript")
    if transcript:
        print(
            f"  транскрипт ({transcript['asr_model']}, {transcript['latency_ms'] / 1000:.1f} с): "
            f"{preview(transcript['text'])}"
        )
    evaluation = body.get("evaluation")
    if not evaluation:
        print(f"  оценки нет: {body['error']}")
        return
    scores = ", ".join(f"{s['criterion']} {s['score']}" for s in evaluation["scores"])
    print(f"  итог {evaluation['total']} ({scores}), {evaluation['latency_ms'] / 1000:.1f} с")
    for doubt in evaluation["asr_doubts"]:
        print(f"  сомнение: {doubt}")


async def reference_run(
    settings: Settings,
    session_id: uuid.UUID,
    references: dict[str, str],
    model: str,
    timeout_s: float,
) -> uuid.UUID:
    """`references` — ответ → текст ручной расшифровки."""
    engine = make_engine(settings.database_url)
    queue = ArqQueue(settings.redis_url)
    maker = make_sessionmaker(engine)
    try:
        async with maker() as db:
            for answer_id, text in references.items():
                await add_reference_transcript(db, uuid.UUID(answer_id), text)
            run = await create_run(
                db,
                name="reference",
                provider=settings.llm_config("eval").provider,
                model=model,
                run_filter=RunFilter(session_id=session_id),
                transcript_kind="reference",
            )
        await queue.enqueue_run(run.id)
        deadline = time.monotonic() + timeout_s
        while True:
            async with maker() as db:
                run = await get_run(db, run.id)
            if run.status in ("done", "failed") or time.monotonic() > deadline:
                break
            await asyncio.sleep(POLL_S)
        print(f"\nпрогон по reference: {run.status}, готово {run.done}/{run.total}")

        async with maker() as db:
            rows = await select_rows(
                db, ExportFilter(run_ids=[run.id], live=True, session_id=session_id)
            )
        csv_path = DATA / f"{session_id}.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as out:
            write_csv(out, rows, "wide")
        print(f"CSV: {csv_path.relative_to(ROOT)}")
        for row in rows:
            kind = row.transcript.kind if row.transcript else "-"
            print(f"  {row.answer.id}  {kind:9}  итог {row.evaluation.total}")
        return run.id
    finally:
        await queue.aclose()
        await engine.dispose()


async def through_api(settings: Settings, vacancy_name: str, timeout_s: float) -> None:
    files = samples()[:2]
    async with httpx.AsyncClient(base_url=API, timeout=300) as api:
        eval_model = (await api.get("/health/llm")).json()["roles"]["eval"]["model"]
        user = (await api.put("/users/telegram/900004")).json()["id"]
        text = (VACANCIES / f"{vacancy_name}.txt").read_text(encoding="utf-8")
        started = time.perf_counter()
        vacancy = await api.post("/vacancies", json={"user_id": user, "text": text})
        vacancy.raise_for_status()
        response = await api.post(
            "/sessions", json={"vacancy_id": vacancy.json()["id"], "n": len(files) + 1}
        )
        response.raise_for_status()
        session = response.json()
        print(f"вакансия и сессия: {time.perf_counter() - started:.0f} с")

        answers: dict[Path, str] = {}
        for path, question in zip(files, session["questions"], strict=False):
            response = await api.post(
                f"/sessions/{session['id']}/questions/{question['id']}/answer/voice",
                files={"file": (path.name, path.read_bytes(), "audio/ogg")},
            )
            response.raise_for_status()
            answers[path] = response.json()["answer_id"]
        references: dict[str, str] = {}
        for path, answer_id in answers.items():
            print(f"\n{path.name}")
            body = await wait_answer(api, answer_id, timeout_s)
            print_result(body)
            txt = path.with_suffix(".txt")
            text = txt.read_text(encoding="utf-8") if txt.exists() else ""
            if not text.strip():  # 004 §16 п. 4: расшифровка — распознанный текст без правки
                text = (body.get("transcript") or {}).get("text") or ""
            if text.strip():
                references[answer_id] = text

    await reference_run(settings, uuid.UUID(session["id"]), references, eval_model, timeout_s * 2)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr-only", action="store_true", help="только замер распознавания")
    parser.add_argument("--model", help="модель whisper для --asr-only; по умолчанию ASR_MODEL")
    parser.add_argument(
        "--vacancy", default="python_backend", help="файл из research/data/vacancies"
    )
    parser.add_argument("--timeout", type=float, default=600, help="ожидание одного ответа, с")
    args = parser.parse_args()
    settings = Settings()
    if args.asr_only:
        await asr_only(settings, args.model or settings.asr_model)
    else:
        await through_api(settings, args.vacancy, args.timeout)


if __name__ == "__main__":
    asyncio.run(main())
