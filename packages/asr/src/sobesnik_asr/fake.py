"""Фейковый распознаватель для тестов и `tools`."""

from sobesnik_asr.base import Transcription


class FakeTranscriber:
    """Возвращает заданный текст или бросает заданную ошибку. Вызовы видны в `calls`."""

    name = "fake"

    def __init__(
        self,
        text: str = "Индекс B-tree упорядочен по первому столбцу.",
        *,
        model: str = "fake",
        language: str = "ru",
        duration_s: float = 10.0,
        error: Exception | None = None,
    ) -> None:
        self.model = model
        self.text = text
        self.language = language
        self.duration_s = duration_s
        self.error = error
        self.calls: list[tuple[int, str | None]] = []
        """(размер аудио, язык) каждого вызова."""

    async def transcribe(self, audio: bytes, *, language: str | None) -> Transcription:
        self.calls.append((len(audio), language))
        if self.error is not None:
            raise self.error
        return Transcription(
            text=self.text,
            language=language or self.language,
            language_probability=None if language else 0.99,
            duration_s=self.duration_s,
            avg_logprob=-0.2,
            latency_ms=5,
            model=self.model,
            params={"language": language},
        )

    async def aclose(self) -> None:
        return None
