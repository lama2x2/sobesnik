"""Проверка аудиофайла до сохранения: формат по содержимому и длительность (004 §5.2)."""

import io
from dataclasses import dataclass
from typing import Literal

import av

from sobesnik_asr.base import AudioDecodeError

AudioFormat = Literal["ogg", "webm", "mp3", "wav", "m4a"]

# имя формата PyAV (первое в списке через запятую) → формат и mime
_FORMATS: dict[str, tuple[AudioFormat, str]] = {
    "ogg": ("ogg", "audio/ogg"),
    "matroska": ("webm", "audio/webm"),
    "mp3": ("mp3", "audio/mpeg"),
    "wav": ("wav", "audio/wav"),
    "mov": ("m4a", "audio/mp4"),
}


@dataclass(frozen=True)
class AudioInfo:
    format: AudioFormat
    mime: str
    duration_s: float


def _decoded_duration(container: av.container.InputContainer) -> float:
    """Длительность по декодированным кадрам — если контейнер её не знает (бывает у ogg)."""
    stream = container.streams.audio[0]
    samples = 0
    for frame in container.decode(stream):
        samples += frame.samples
    return samples / stream.rate if stream.rate else 0.0


def probe_audio(data: bytes) -> AudioInfo:
    """Формат и длительность. Не аудио или формат не из списка ANS-2 — `AudioDecodeError`."""
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            name = container.format.name.split(",")[0]
            if name not in _FORMATS:
                raise AudioDecodeError(f"формат не поддерживается: {name}")
            if not container.streams.audio:
                raise AudioDecodeError("в файле нет звука")
            if container.duration is not None and container.duration > 0:
                duration = container.duration / av.time_base
            else:
                duration = _decoded_duration(container)
    except AudioDecodeError:
        raise
    except (av.FFmpegError, ValueError, IndexError) as exc:
        raise AudioDecodeError("файл не читается как аудио") from exc
    fmt, mime = _FORMATS[name]
    return AudioInfo(format=fmt, mime=mime, duration_s=round(duration, 2))
