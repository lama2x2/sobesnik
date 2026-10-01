"""Провайдер OpenAI-совместимого API: POST {base_url}/chat/completions без стриминга.

Работает с облачными endpoint'ами и с `/v1` самой Ollama.
"""

import json
import time
from typing import Any

import httpx

from sobesnik_llm.base import LLMError, LLMHealth, LLMRequest, LLMResponse, LLMUnavailableError
from sobesnik_llm.config import StructuredMode


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout_s: float = 120.0,
        structured: StructuredMode = "json_schema",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._structured = structured
        self._client = client or httpx.AsyncClient()
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_s, connect=5.0)
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        system = request.system
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.json_schema is not None:
            if self._structured == "json_schema":
                # strict: false — строгий режим требует, чтобы все поля были required
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer",
                        "schema": request.json_schema,
                        "strict": False,
                    },
                }
            else:
                payload["response_format"] = {"type": "json_object"}
                schema = json.dumps(request.json_schema, ensure_ascii=False)
                system = f"{system}\n\nОтвет — только JSON по этой JSON Schema:\n{schema}"
        payload["messages"] = [
            {"role": "system", "content": system},
            {"role": "user", "content": request.prompt},
        ]
        return payload

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        response = await self._request(
            "POST", "/chat/completions", json=self.build_payload(request)
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        self._raise_for_status(response)
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("content не строка")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("неожиданный формат ответа OpenAI-совместимого API") from exc
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=data.get("model") or self.model,
            latency_ms=latency_ms,
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
        )

    async def health(self) -> LLMHealth:
        try:
            response = await self._request("GET", "/models")
        except LLMUnavailableError as exc:
            return self._health(ok=False, detail=str(exc))
        if response.status_code in (401, 403):
            return self._health(ok=False, detail="неверный ключ API")
        if response.status_code == 404:
            return self._health(ok=True, detail="список моделей недоступен")
        if response.status_code != 200:
            return self._health(ok=False, detail=f"endpoint ответил {response.status_code}")
        try:
            ids = {m.get("id") for m in response.json().get("data", [])}
        except (ValueError, AttributeError, TypeError):
            return self._health(ok=True, detail="список моделей недоступен")
        if not ids:
            return self._health(ok=True, detail="список моделей недоступен")
        if self.model not in ids and f"{self.model}:latest" not in ids:
            return self._health(ok=False, detail=f"модель {self.model} не найдена")
        return self._health(ok=True)

    def _health(self, *, ok: bool, detail: str | None = None) -> LLMHealth:
        return LLMHealth(ok=ok, provider=self.name, model=self.model, detail=detail)

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status == 200:
            return
        if status in (401, 403):
            raise LLMError("неверный ключ API")
        if status == 404:
            raise LLMUnavailableError(f"модель {self.model} не найдена")
        if status == 429 or status >= 500:
            raise LLMUnavailableError(f"endpoint ответил {status}")
        raise LLMError(f"endpoint ответил {status}: {response.text[:200]}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                self._base_url + path,
                headers=self._headers,
                timeout=self._timeout,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError("таймаут запроса к LLM") from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(f"нет соединения с LLM: {exc}") from exc
