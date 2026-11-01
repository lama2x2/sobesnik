"""Эталонные расшифровки голосовых ответов датасета моделью whisper large-v3 (005 §12).

    docker compose run --rm tools uv run python research/quality/reference_asr.py --dataset voice-10

Для каждого голосового ответа датасета без `reference` распознаёт его аудио из
research/data/asr_check/<external_id>.ogg моделью large-v3 из кэша Hugging Face хоста
(смонтирован в /hf только для чтения, ничего не качается) и добавляет транскрипт `reference`
с `params = {"from_model": "large-v3"}`. Это расхождение двух распознавателей, а не ручная
расшифровка: так решил автор (005 §18 п. 2). В консоль — только имена и время, не тексты.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from sobesnik_api.db.models import Answer, Audio, DatasetItem, Transcript
from sobesnik_api.db.session import make_engine, make_sessionmaker
from sobesnik_api.services.datasets import get_dataset
from sobesnik_api.services.voice import add_reference_transcript
from sobesnik_api.settings import Settings
from sobesnik_asr import FasterWhisperTranscriber

ROOT = Path(__file__).resolve().parents[2]
AUDIO = ROOT / "research" / "data" / "asr_check"
HF = Path("/hf")
MODEL = "large-v3"


def model_path() -> str:
    snapshots = sorted(HF.glob(f"models--Systran--faster-whisper-{MODEL}/snapshots/*/model.bin"))
    if not snapshots:
        raise SystemExit(f"нет {MODEL} в {HF}: смонтирован ли кэш Hugging Face хоста?")
    return str(snapshots[-1].parent)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", required=True)
    args = parser.parse_args()

    settings = Settings()
    engine = make_engine(settings.database_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as db:
        dataset = await get_dataset(db, args.dataset)
        has_reference = (
            select(Transcript.id)
            .join(Audio, Audio.id == Transcript.audio_id)
            .where(Audio.answer_id == Answer.id, Transcript.kind == "reference")
            .exists()
        )
        rows = list(
            await db.execute(
                select(Answer.id, DatasetItem.external_id)
                .join(DatasetItem, DatasetItem.id == Answer.dataset_item_id)
                .where(DatasetItem.dataset_id == dataset.id, Answer.kind == "voice")
                .where(~has_reference)
                .order_by(DatasetItem.external_id)
            )
        )
    if not rows:
        print("эталоны уже есть")
        return
    transcriber = FasterWhisperTranscriber(model_path(), beam_size=5)
    for answer_id, external_id in rows:
        audio = AUDIO / f"{external_id}.ogg"
        result = await transcriber.transcribe(audio.read_bytes(), language="ru")
        async with sessionmaker() as db:
            await add_reference_transcript(
                db, answer_id, result.text, "ru", {"from_model": MODEL, **result.params}
            )
        speed = result.latency_ms / 1000 / result.duration_s * 60
        print(f"{external_id}: {result.duration_s:.0f} с аудио, {speed:.0f} с на минуту речи")
        sys.stdout.flush()
    await transcriber.aclose()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
