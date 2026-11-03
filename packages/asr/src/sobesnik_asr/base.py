"""Единый интерфейс распознавания речи (004 §5.1)."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Transcription:
    text: str
    language: str
    language_probability: float | None
    duration_s: float
    """Длительность аудио по декодеру."""
    avg_logprob: float | None
    """Средняя уверенность по сегментам, взвешенная их длиной."""
    latency_ms: int
    model: str
    params: dict[str, Any] = field(default_factory=dict)
    """Параметры распознавания: по ним его можно повторить."""


class ASRError(Exception):
    """Сбой распознавания, который может пройти при повторе (модель не скачалась и т. п.)."""


class AudioDecodeError(ASRError):
    """Файл не читается как аудио: повтор не поможет."""


class Transcriber(Protocol):
    name: str
    model: str

    async def transcribe(self, audio: bytes, *, language: str | None) -> Transcription:
        """`language=None` — автоопределение языка."""
        ...

    async def prefetch(self) -> None:
        """Скачать модель заранее, не загружая её в память (008 §7). Сбой — `ASRError`."""
        ...

    async def aclose(self) -> None: ...
