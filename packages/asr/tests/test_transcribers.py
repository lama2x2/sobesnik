from dataclasses import dataclass
from typing import Any

import pytest

from sobesnik_asr import (
    ASRConfig,
    ASRError,
    FakeTranscriber,
    FasterWhisperTranscriber,
    make_transcriber,
)


@dataclass
class Segment:
    text: str
    start: float
    end: float
    avg_logprob: float


@dataclass
class Info:
    language: str = "ru"
    language_probability: float = 0.97
    duration: float = 12.0


class StubWhisper:
    """Подмена WhisperModel: веса не загружаются."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Any, Info]:
        self.calls.append(kwargs)
        segments = [Segment(" Индекс ", 0.0, 3.0, -0.1), Segment("упорядочен.", 3.0, 4.0, -0.5)]
        return iter(segments), Info()


async def test_whisper_params_and_result() -> None:
    stub = StubWhisper()
    created: list[int] = []

    def factory() -> StubWhisper:
        created.append(1)
        return stub

    asr = FasterWhisperTranscriber("small", beam_size=3, model_factory=factory)
    result = await asr.transcribe(b"audio", language="ru")
    await asr.transcribe(b"audio", language=None)

    assert created == [1]  # модель загружается один раз
    assert stub.calls[0] == {"language": "ru", "beam_size": 3, "vad_filter": True}
    assert stub.calls[1]["language"] is None
    assert result.text == "Индекс упорядочен."
    assert result.language == "ru"
    assert result.duration_s == 12.0
    assert result.avg_logprob == pytest.approx((-0.1 * 3 - 0.5 * 1) / 4, abs=1e-4)
    assert result.model == "small"
    assert result.params["compute_type"] == "int8"


async def test_whisper_load_failure_is_asr_error() -> None:
    def factory() -> Any:
        raise OSError("нет сети")

    asr = FasterWhisperTranscriber("small", model_factory=factory)
    with pytest.raises(ASRError):
        await asr.transcribe(b"audio", language="ru")


async def test_fake_transcriber() -> None:
    fake = FakeTranscriber("текст")
    result = await fake.transcribe(b"abc", language="ru")
    assert result.text == "текст"
    assert fake.calls == [(3, "ru")]
    fake.error = ASRError("сбой")
    with pytest.raises(ASRError):
        await fake.transcribe(b"abc", language="ru")


def test_factory() -> None:
    assert isinstance(make_transcriber(ASRConfig(provider="fake")), FakeTranscriber)
    whisper = make_transcriber(ASRConfig(model="medium"))
    assert isinstance(whisper, FasterWhisperTranscriber)
    assert whisper.model == "medium"
