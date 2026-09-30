"""Фейковый провайдер для тестов: отдаёт заранее заданные ответы."""

from collections.abc import Callable, Iterable

from sobesnik_llm.base import LLMHealth, LLMRequest, LLMResponse, LLMUnavailableError

Reply = str | Callable[[LLMRequest], str] | Exception


class FakeProvider:
    """Ответы берутся по очереди из `replies`; последний повторяется, когда очередь кончилась.

    Ответ — строка, функция от запроса или исключение, которое надо бросить.
    Все полученные запросы сохраняются в `requests`.
    """

    name = "fake"

    def __init__(self, replies: Iterable[Reply] = (), *, model: str = "fake-model") -> None:
        self.model = model
        self.replies: list[Reply] = list(replies)
        self.requests: list[LLMRequest] = []
        self.available = True

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.available:
            raise LLMUnavailableError("фейковый провайдер выключен")
        if not self.replies:
            raise AssertionError("FakeProvider: ответы не заданы")
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        text = reply(request) if callable(reply) else reply
        return LLMResponse(text=text, provider=self.name, model=self.model, latency_ms=0)

    async def health(self) -> LLMHealth:
        detail = None if self.available else "фейковый провайдер выключен"
        return LLMHealth(ok=self.available, provider=self.name, model=self.model, detail=detail)

    async def aclose(self) -> None:
        pass
