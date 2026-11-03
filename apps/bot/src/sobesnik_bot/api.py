"""Клиент API Sobesnik (006 §7.1). Бот не импортирует `sobesnik_api`: модели ответов — свои,
только нужные боту поля."""

import uuid
from datetime import datetime
from typing import Any, Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict

TIMEOUT_S = 150.0
"""С запасом над таймаутом LLM в API (120 с): разбор и генерация идут синхронно."""

Level = Literal["intern", "junior", "middle", "senior", "lead"]
LEVELS: tuple[Level, ...] = ("intern", "junior", "middle", "senior", "lead")


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class User(_Model):
    id: uuid.UUID


class Requirement(_Model):
    id: uuid.UUID
    text: str
    is_required: bool
    topic: str | None
    topic_title: str | None = None


class Vacancy(_Model):
    id: uuid.UUID
    title: str
    level: Level
    stack: list[str]
    requirements: list[Requirement]


class SessionAnswer(_Model):
    id: uuid.UUID
    kind: str
    status: str
    total: int | None
    passed: bool | None


class SessionQuestion(_Model):
    id: uuid.UUID
    position: int
    text: str
    topic: str | None
    answer: SessionAnswer | None = None
    docs: list["Doc"] = []


class Session(_Model):
    id: uuid.UUID
    vacancy_id: uuid.UUID | None
    mode: str = "vacancy"
    status: str
    questions: list[SessionQuestion]


class Accepted(_Model):
    answer_id: uuid.UUID
    status: str


class CriterionScore(_Model):
    criterion: str
    title: str
    score: int
    reason: str


class Evaluation(_Model):
    total: int
    passed: bool
    scores: list[CriterionScore]
    missed_points: list[str]
    advice: str
    asr_doubts: list[str] = []


class Transcript(_Model):
    text: str


class Answer(_Model):
    id: uuid.UUID
    session_id: uuid.UUID | None
    question_id: uuid.UUID
    kind: str
    status: str
    error: str | None
    transcript: Transcript | None = None
    evaluation: Evaluation | None = None
    topic: str | None = None
    docs: list["Doc"] = []


class Doc(_Model):
    title: str
    url: str


class SummaryQuestion(_Model):
    position: int
    question_id: uuid.UUID
    answer_status: str | None
    total: int | None
    passed: bool | None


class Summary(_Model):
    questions: list[SummaryQuestion]
    answered: int
    evaluated: int
    passed: int


class Forgotten(_Model):
    audio_deleted: int
    transcripts_deleted: int
    answers_deleted: int = 0
    sessions_deleted: int = 0
    vacancies_deleted: int = 0


class TopicProgress(_Model):
    slug: str
    title: str
    status: Literal["weak", "few", "ok"]
    low: bool
    attempts: int
    last_score: int | None
    avg_last3: float | None
    docs: list[Doc]


class SessionHistory(_Model):
    created_at: datetime
    mode: str
    vacancy_title: str | None
    answered: int
    passed: int
    avg_total: float | None
    scores: list[int | None]


class Progress(_Model):
    topics: list[TopicProgress]
    sessions: list[SessionHistory]


class WebLink(_Model):
    """Одноразовый вход в веб-кабинет (009 §5.2): секрет — в адрес ссылки, код — с телефона."""

    secret: str
    code: str
    expires_at: datetime


class LlmStatus(_Model):
    """`GET /health/llm`, сжатый до того, что нужно боту (008 §7)."""

    ok: bool
    model: str | None = None
    hint: str | None = None


class ApiError(Exception):
    """Ответ API с ошибкой: `{"error": {"code", "message", "hint"?}}` или стандартный `422`
    FastAPI."""

    def __init__(self, status: int, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint
        """Что сделать человеку (008 §7): есть у ошибок модели, которые он может исправить сам."""


class ApiUnavailable(ApiError):
    """API не ответил: сеть, таймаут или 5xx без кода приложения."""

    def __init__(self, message: str) -> None:
        super().__init__(0, "api_unavailable", message)


class Api(Protocol):
    async def llm_status(self) -> LlmStatus: ...

    async def upsert_user(self, telegram_id: int) -> User: ...

    async def create_vacancy(
        self,
        user_id: uuid.UUID,
        *,
        text: str | None = None,
        url: str | None = None,
        html: str | None = None,
    ) -> Vacancy: ...

    async def set_level(self, vacancy_id: uuid.UUID, level: Level) -> Vacancy: ...

    async def get_vacancy(self, vacancy_id: uuid.UUID) -> Vacancy: ...

    async def create_session(
        self, vacancy_id: uuid.UUID, n: int, topics: list[str] | None = None
    ) -> Session: ...

    async def create_repeat(self, user_id: uuid.UUID, n: int) -> Session: ...

    async def get_progress(self, user_id: uuid.UUID) -> Progress: ...

    async def get_session(self, session_id: uuid.UUID) -> Session: ...

    async def finish_session(self, session_id: uuid.UUID) -> Summary: ...

    async def answer_text(
        self, session_id: uuid.UUID, question_id: uuid.UUID, text: str
    ) -> Accepted: ...

    async def answer_voice(
        self,
        session_id: uuid.UUID,
        question_id: uuid.UUID,
        data: bytes,
        filename: str,
        mime: str,
    ) -> Accepted: ...

    async def get_answer(self, answer_id: uuid.UUID) -> Answer: ...

    async def forget_audio(self, user_id: uuid.UUID) -> Forgotten: ...

    async def forget_all(self, user_id: uuid.UUID) -> Forgotten: ...

    async def web_link(self, user_id: uuid.UUID) -> WebLink: ...

    async def aclose(self) -> None: ...


M = TypeVar("M", bound=BaseModel)


def _error(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        hint = err.get("hint")
        return ApiError(
            response.status_code,
            str(err.get("code")),
            str(err.get("message")),
            hint if isinstance(hint, str) else None,
        )
    if response.status_code == 422:  # проверка запроса FastAPI
        return ApiError(422, "validation_error", "запрос не прошёл проверку")
    if response.status_code >= 500:
        return ApiUnavailable(f"API ответил {response.status_code}")
    return ApiError(response.status_code, "error", f"API ответил {response.status_code}")


class ApiClient:
    def __init__(
        self, base_url: str, token: str | None = None, *, client: httpx.AsyncClient | None = None
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
        self._client.headers.update(headers)
        self._base = base_url.rstrip("/")

    async def _call(self, model: type[M], method: str, path: str, **kwargs: Any) -> M:
        try:
            response = await self._client.request(method, self._base + path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiUnavailable(type(exc).__name__) from exc
        if response.is_error:
            raise _error(response)
        return model.model_validate(response.json())

    async def llm_status(self) -> LlmStatus:
        """503 здесь — не ошибка, а ответ «модель не готова» с подсказкой в теле."""
        try:
            response = await self._client.get(self._base + "/health/llm")
            roles = response.json()["roles"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ApiUnavailable(type(exc).__name__) from exc
        bad = [r for r in roles.values() if not r.get("ok")]
        role = bad[0] if bad else next(iter(roles.values()), {})
        return LlmStatus(ok=not bad, model=role.get("model"), hint=role.get("hint"))

    async def upsert_user(self, telegram_id: int) -> User:
        return await self._call(User, "PUT", f"/users/telegram/{telegram_id}")

    async def create_vacancy(
        self,
        user_id: uuid.UUID,
        *,
        text: str | None = None,
        url: str | None = None,
        html: str | None = None,
    ) -> Vacancy:
        body: dict[str, str] = {"user_id": str(user_id)}
        for key, value in (("text", text), ("url", url), ("html", html)):
            if value is not None:
                body[key] = value
        return await self._call(Vacancy, "POST", "/vacancies", json=body)

    async def set_level(self, vacancy_id: uuid.UUID, level: Level) -> Vacancy:
        return await self._call(Vacancy, "PATCH", f"/vacancies/{vacancy_id}", json={"level": level})

    async def get_vacancy(self, vacancy_id: uuid.UUID) -> Vacancy:
        return await self._call(Vacancy, "GET", f"/vacancies/{vacancy_id}")

    async def create_session(
        self, vacancy_id: uuid.UUID, n: int, topics: list[str] | None = None
    ) -> Session:
        body: dict[str, Any] = {"vacancy_id": str(vacancy_id), "n": n}
        if topics is not None:
            body["topics"] = topics
        return await self._call(Session, "POST", "/sessions", json=body)

    async def create_repeat(self, user_id: uuid.UUID, n: int) -> Session:
        body = {"mode": "repeat", "user_id": str(user_id), "n": n}
        return await self._call(Session, "POST", "/sessions", json=body)

    async def get_progress(self, user_id: uuid.UUID) -> Progress:
        return await self._call(Progress, "GET", f"/users/{user_id}/progress")

    async def get_session(self, session_id: uuid.UUID) -> Session:
        return await self._call(Session, "GET", f"/sessions/{session_id}")

    async def finish_session(self, session_id: uuid.UUID) -> Summary:
        return await self._call(Summary, "POST", f"/sessions/{session_id}/finish")

    async def answer_text(
        self, session_id: uuid.UUID, question_id: uuid.UUID, text: str
    ) -> Accepted:
        path = f"/sessions/{session_id}/questions/{question_id}/answer"
        return await self._call(Accepted, "POST", path, json={"text": text})

    async def answer_voice(
        self,
        session_id: uuid.UUID,
        question_id: uuid.UUID,
        data: bytes,
        filename: str,
        mime: str,
    ) -> Accepted:
        path = f"/sessions/{session_id}/questions/{question_id}/answer/voice"
        return await self._call(Accepted, "POST", path, files={"file": (filename, data, mime)})

    async def get_answer(self, answer_id: uuid.UUID) -> Answer:
        return await self._call(Answer, "GET", f"/answers/{answer_id}")

    async def forget_audio(self, user_id: uuid.UUID) -> Forgotten:
        path = f"/users/{user_id}/data"
        return await self._call(Forgotten, "DELETE", path, params={"scope": "audio"})

    async def forget_all(self, user_id: uuid.UUID) -> Forgotten:
        path = f"/users/{user_id}/data"
        return await self._call(Forgotten, "DELETE", path, params={"scope": "all"})

    async def web_link(self, user_id: uuid.UUID) -> WebLink:
        return await self._call(WebLink, "POST", "/auth/links", json={"user_id": str(user_id)})

    async def aclose(self) -> None:
        await self._client.aclose()
