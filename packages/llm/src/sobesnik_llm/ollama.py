"""Провайдер Ollama: POST /api/chat без стриминга."""

import time
from typing import Any

import httpx

from sobesnik_llm.base import LLMError, LLMHealth, LLMRequest, LLMResponse, LLMUnavailableError


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = 120.0,
        num_ctx: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._num_ctx = num_ctx
        self._client = client or httpx.AsyncClient()
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_s, connect=5.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": request.temperature}
        if request.seed is not None:
            options["seed"] = request.seed
        if self._num_ctx is not None:
            options["num_ctx"] = self._num_ctx
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            "stream": False,
            "think": False,
            "options": options,
        }
        if request.json_schema is not None:
            payload["format"] = request.json_schema
        return payload

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        response = await self._request("POST", "/api/chat", json=self.build_payload(request))
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code == 404:
            raise LLMUnavailableError(f"модель {self.model} не найдена в Ollama")
        if response.status_code >= 500:
            raise LLMUnavailableError(f"Ollama ответила {response.status_code}")
        if response.status_code != 200:
            raise LLMError(f"Ollama ответила {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
            text = data["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError("неожиданный формат ответа Ollama") from exc
        return LLMResponse(
            text=text,
            provider=self.name,
            model=data.get("model", self.model),
            latency_ms=latency_ms,
            tokens_in=data.get("prompt_eval_count"),
            tokens_out=data.get("eval_count"),
        )

    async def health(self) -> LLMHealth:
        try:
            response = await self._request("GET", "/api/tags")
            response.raise_for_status()
            names = {m.get("name") for m in response.json().get("models", [])}
        except (LLMUnavailableError, httpx.HTTPError, ValueError) as exc:
            return self._health(ok=False, detail=f"Ollama недоступна: {exc}")
        if self.model not in names and f"{self.model}:latest" not in names:
            return self._health(ok=False, detail=f"модель {self.model} не скачана")
        return self._health(ok=True)

    def _health(self, *, ok: bool, detail: str | None = None) -> LLMHealth:
        return LLMHealth(ok=ok, provider=self.name, model=self.model, detail=detail)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(
                method, self._base_url + path, timeout=self._timeout, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError("таймаут запроса к Ollama") from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(f"нет соединения с Ollama: {exc}") from exc
