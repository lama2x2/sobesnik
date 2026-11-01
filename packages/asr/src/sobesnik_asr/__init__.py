"""Распознавание речи и хранилище аудио: единые интерфейсы, выбор реализации через env."""

from sobesnik_asr.base import ASRError, AudioDecodeError, Transcriber, Transcription
from sobesnik_asr.factory import ASRConfig, ASRProviderName, make_transcriber
from sobesnik_asr.fake import FakeTranscriber
from sobesnik_asr.probe import AudioFormat, AudioInfo, probe_audio
from sobesnik_asr.storage import (
    AudioNotFoundError,
    AudioStorage,
    MemoryStorage,
    S3AudioStorage,
    StorageError,
    StorageUnavailableError,
)
from sobesnik_asr.whisper import FasterWhisperTranscriber

__all__ = [
    "ASRConfig",
    "ASRError",
    "ASRProviderName",
    "AudioDecodeError",
    "AudioFormat",
    "AudioInfo",
    "AudioNotFoundError",
    "AudioStorage",
    "FakeTranscriber",
    "FasterWhisperTranscriber",
    "MemoryStorage",
    "S3AudioStorage",
    "StorageError",
    "StorageUnavailableError",
    "Transcriber",
    "Transcription",
    "make_transcriber",
    "probe_audio",
]
