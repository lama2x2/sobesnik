"""Сегменты полных записей собеседований с таймингами (005 §5.3): по ним ищутся вопросы.

    # на хосте: моно-звук полной записи
    ffmpeg -i ЗАПИСЬ -vn -ac 1 -ar 16000 -c:a libopus -b:a 24k research/data/asr_check/full/ИМЯ.ogg
    docker compose run --rm tools uv run python research/asr_check/find_questions.py

Каждый `full/*.ogg` распознаётся whisper `small` (как в бою) и пишется в `full/ИМЯ.segments.jsonl`:
строка на сегмент `{"start", "end", "text"}`. Уже распознанные записи пропускаются.
В консоль — только прогресс: в записях есть голоса интервьюеров, тексты в лог не попадают.
"""

import argparse
import json
import time
from pathlib import Path

from faster_whisper import WhisperModel  # type: ignore[import-untyped]

FULL = Path(__file__).resolve().parents[2] / "research" / "data" / "asr_check" / "full"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", default="small")
    parser.add_argument("--model-dir", default="/models")
    args = parser.parse_args()

    model = WhisperModel(
        args.model, device="cpu", compute_type="int8", download_root=args.model_dir
    )
    for audio in sorted(FULL.glob("*.ogg")):
        out = audio.with_suffix(".segments.jsonl")
        if out.exists():
            print(f"{audio.name}: уже есть", flush=True)
            continue
        started = time.perf_counter()
        segments, info = model.transcribe(str(audio), language="ru", beam_size=5, vad_filter=True)
        tmp = out.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for i, seg in enumerate(segments):
                row = {"start": round(seg.start, 1), "end": round(seg.end, 1), "text": seg.text}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                if i % 100 == 0:
                    print(
                        f"{audio.name}: {seg.end / 60:.0f}/{info.duration / 60:.0f} мин", flush=True
                    )
        tmp.rename(out)
        print(f"{audio.name}: готово за {time.perf_counter() - started:.0f} с", flush=True)


if __name__ == "__main__":
    main()
