"""Живой тест распознавания (004 §13): `pytest -m asr`. Нужна запись в research/data/asr_check/
(research/asr_check/cut.sh) и модель whisper — скачается в ASR_MODEL_DIR при первом запуске."""

import os
from pathlib import Path

import pytest

from sobesnik_asr import FasterWhisperTranscriber

SAMPLES = Path(__file__).resolve().parents[3] / "research" / "data" / "asr_check"
# /models — том asr-models в compose: модель не скачивается заново при каждом запуске
MODEL_DIR = os.environ.get("ASR_MODEL_DIR", "/models")


@pytest.mark.asr
async def test_transcribe_real_recording() -> None:
    files = sorted(SAMPLES.glob("*.ogg"))
    if not files:
        pytest.skip("нет записей в research/data/asr_check")
    asr = FasterWhisperTranscriber(os.environ.get("ASR_MODEL", "small"), model_dir=MODEL_DIR)
    result = await asr.transcribe(files[0].read_bytes(), language="ru")
    assert result.text.strip()
    assert result.language == "ru"
    assert result.duration_s > 0
