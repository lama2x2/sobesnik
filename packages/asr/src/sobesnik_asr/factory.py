"""Выбор распознавателя по настройкам."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from sobesnik_asr.base import Transcriber
from sobesnik_asr.fake import FakeTranscriber
from sobesnik_asr.whisper import FasterWhisperTranscriber

ASRProviderName = Literal["faster_whisper", "fake"]


class ASRConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: ASRProviderName = "faster_whisper"
    model: str = "small"
    model_dir: str | None = None
    compute_type: str = "int8"
    beam_size: int = 5
    cpu_threads: int = 0


def make_transcriber(config: ASRConfig) -> Transcriber:
    match config.provider:
        case "faster_whisper":
            return FasterWhisperTranscriber(
                config.model,
                model_dir=config.model_dir,
                compute_type=config.compute_type,
                beam_size=config.beam_size,
                cpu_threads=config.cpu_threads,
            )
        case "fake":
            return FakeTranscriber(model=config.model)
    raise ValueError(f"неизвестный распознаватель: {config.provider}")
