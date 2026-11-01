"""Распознавание faster-whisper на CPU (004 §5.1)."""

import asyncio
import io
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import av

from sobesnik_asr.base import ASRError, AudioDecodeError, Transcription

log = logging.getLogger(__name__)

ModelFactory = Callable[[], Any]
"""Создаёт объект с методом `transcribe`, как у `faster_whisper.WhisperModel`.
Подменяется в тестах."""


class FasterWhisperTranscriber:
    """Модель загружается при первом вызове и остаётся в памяти.

    Распознавание тяжёлое и синхронное, поэтому идёт в отдельном потоке: цикл событий
    не блокируется.
    """

    name = "faster_whisper"

    def __init__(
        self,
        model: str = "small",
        *,
        model_dir: str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 5,
        cpu_threads: int = 0,
        vad_filter: bool = True,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.model = model
        self._model_dir = model_dir
        self._device = device
        self._compute_type = compute_type
        self._beam_size = beam_size
        self._cpu_threads = cpu_threads
        self._vad_filter = vad_filter
        self._factory = model_factory or self._default_factory
        self._whisper: Any = None
        self._lock = threading.Lock()

    def _default_factory(self) -> Any:
        # тяжёлый импорт — только когда нужна модель
        from faster_whisper import WhisperModel  # type: ignore[import-untyped]

        return WhisperModel(
            self.model,
            device=self._device,
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads,
            download_root=self._model_dir,
        )

    def _load(self) -> Any:
        with self._lock:
            if self._whisper is None:
                started = time.perf_counter()
                try:
                    self._whisper = self._factory()
                except Exception as exc:
                    raise ASRError(f"модель {self.model} не загрузилась: {exc}") from exc
                log.info(
                    "asr model loaded",
                    extra={
                        "asr_model": self.model,
                        "load_ms": int((time.perf_counter() - started) * 1000),
                    },
                )
            return self._whisper

    def params(self, language: str | None) -> dict[str, Any]:
        return {
            "language": language,
            "beam_size": self._beam_size,
            "vad_filter": self._vad_filter,
            "compute_type": self._compute_type,
            "device": self._device,
        }

    def _run(self, audio: bytes, language: str | None) -> Transcription:
        whisper = self._load()
        started = time.perf_counter()
        try:
            segments, info = whisper.transcribe(
                io.BytesIO(audio),
                language=language,
                beam_size=self._beam_size,
                vad_filter=self._vad_filter,
            )
            texts: list[str] = []
            weighted, weight = 0.0, 0.0
            for segment in segments:  # распознавание идёт по мере чтения генератора
                texts.append(segment.text.strip())
                length = max(segment.end - segment.start, 0.0)
                weighted += segment.avg_logprob * length
                weight += length
        except (av.FFmpegError, IndexError) as exc:
            raise AudioDecodeError("файл не читается как аудио") from exc
        except Exception as exc:
            raise ASRError(f"сбой распознавания: {type(exc).__name__}") from exc
        return Transcription(
            text=" ".join(t for t in texts if t),
            language=info.language,
            language_probability=info.language_probability,
            duration_s=round(float(info.duration), 2),
            avg_logprob=round(weighted / weight, 4) if weight else None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=self.model,
            params=self.params(language),
        )

    async def transcribe(self, audio: bytes, *, language: str | None) -> Transcription:
        return await asyncio.to_thread(self._run, audio, language)

    async def aclose(self) -> None:
        self._whisper = None
