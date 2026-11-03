"""Клиент API Sobesnik для веба (009 §4). Веб не импортирует `sobesnik_api` и бота: модели ответов —
свои, только нужные страницам поля.

`Gate` — вход (ещё без пользователя), `Api` — запросы от имени вошедшего пользователя: его токен
уходит в API, и API само отдаёт только его данные (009 §5.1).
"""

import uuid
from datetime import datetime
from typing import Any, Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter

TIMEOUT_S = 150.0
"""С запасом над таймаутом LLM в API (120 с): разбор и генерация идут синхронно."""

Level = Literal["intern", "junior", "middle", "senior", "lead"]
Difficulty = Literal["easier", "same", "harder"]
"""Сложность тренировки: сдвиг уровня на шаг (010 §5)."""
LEVELS: tuple[Level, ...] = ("intern", "junior", "middle", "senior", "lead")
FINAL_STATUSES = frozenset({"done", "failed"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Token(_Model):
    token: str
    user_id: uuid.UUID
    expires_at: datetime


class Me(_Model):
    user_id: uuid.UUID
    telegram: bool
    local: bool


class LlmStatus(_Model):
    ok: bool
    model: str | None = None
    hint: str | None = None


class Doc(_Model):
    title: str
    url: str


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

    def topics(self) -> list[tuple[str, str]]:
        """Темы требований по порядку, без повторов: (slug, заголовок)."""
        seen: dict[str, str] = {}
        for r in self.requirements:
            if r.topic and r.topic not in seen:
                seen[r.topic] = r.topic_title or r.topic
        return list(seen.items())


class VacancyBrief(_Model):
    id: uuid.UUID
    title: str
    level: str
    created_at: datetime


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
    docs: list[Doc] = []


class Session(_Model):
    id: uuid.UUID
    vacancy_id: uuid.UUID | None
    mode: str
    status: str
    questions: list[SessionQuestion]

    def question(self, position: int) -> SessionQuestion | None:
        return next((q for q in self.questions if q.position == position), None)

    def first_open(self) -> int:
        """Позиция первого вопроса без ответа (упавший ответ можно дать заново); все отвечены —
        за последним."""
        positions = sorted(
            q.position for q in self.questions if q.answer is None or q.answer.status == "failed"
        )
        return positions[0] if positions else len(self.questions) + 1


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
    created_at: datetime
    transcript: Transcript | None = None
    evaluation: Evaluation | None = None
    docs: list[Doc] = []
    has_review: bool = False

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES


class ReviewPoint(_Model):
    n: int
    text: str
    status: Literal["covered", "missed", "doubtful"]
    comment: str


class ReviewMistake(_Model):
    quote: str
    correction: str


class Review(_Model):
    """Подробный разбор ответа (010 §4)."""

    reference: str
    points: list[ReviewPoint]
    mistakes: list[ReviewMistake]
    question_issue: str | None
    docs: list[Doc]


class SummaryQuestion(_Model):
    position: int
    question_id: uuid.UUID
    answer_status: str | None
    total: int | None
    passed: bool | None


class Summary(_Model):
    id: uuid.UUID
    questions: list[SummaryQuestion]
    answered: int
    evaluated: int
    passed: int


class TopicProgress(_Model):
    slug: str
    title: str
    status: Literal["weak", "few", "ok"]
    low: bool
    attempts: int
    last_score: int | None
    avg_last3: float | None
    docs: list[Doc] = []


class SessionHistory(_Model):
    id: uuid.UUID
    created_at: datetime
    mode: str
    status: str = "finished"
    vacancy_title: str | None
    answered: int
    passed: int
    avg_total: float | None
    scores: list[int | None]


class Progress(_Model):
    topics: list[TopicProgress]
    sessions: list[SessionHistory]


class Forgotten(_Model):
    audio_deleted: int
    transcripts_deleted: int
    answers_deleted: int | None = None
    sessions_deleted: int | None = None
    vacancies_deleted: int | None = None


class ApiError(Exception):
    """Ошибка API: код из `{"error": {"code", "message", "hint"}}`."""

    def __init__(self, status: int, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint


class ApiUnavailable(ApiError):
    """API не ответил или ответил 5xx без понятного кода."""

    def __init__(self, message: str) -> None:
        super().__init__(503, "api_unavailable", message)


class Api(Protocol):
    """Запросы от имени вошедшего пользователя."""

    async def me(self) -> Me: ...

    async def llm_status(self) -> LlmStatus: ...

    async def vacancies(self, user_id: uuid.UUID) -> list[VacancyBrief]: ...

    async def create_vacancy(
        self, *, text: str | None = None, url: str | None = None, html: str | None = None
    ) -> Vacancy: ...

    async def get_vacancy(self, vacancy_id: uuid.UUID) -> Vacancy: ...

    async def set_level(self, vacancy_id: uuid.UUID, level: Level) -> Vacancy: ...

    async def remove_requirement(
        self, vacancy_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> Vacancy: ...

    async def create_session(
        self,
        vacancy_id: uuid.UUID,
        n: int,
        topics: list[str] | None,
        required_only: bool = False,
        difficulty: Difficulty = "same",
    ) -> Session: ...

    async def create_repeat(self, n: int, difficulty: Difficulty = "same") -> Session: ...

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
        content_type: str,
    ) -> Accepted: ...

    async def get_answer(self, answer_id: uuid.UUID) -> Answer: ...

    async def review_answer(self, answer_id: uuid.UUID) -> Review: ...

    async def get_review(self, answer_id: uuid.UUID) -> Review: ...

    async def get_progress(self, user_id: uuid.UUID) -> Progress: ...

    async def forget(self, user_id: uuid.UUID, scope: Literal["audio", "all"]) -> Forgotten: ...

    async def logout(self, *, everywhere: bool = False) -> None: ...


class Gate(Protocol):
    """Вход: погасить ссылку или код из бота, войти без Telegram, получить клиент пользователя."""

    async def redeem(self, *, secret: str | None = None, code: str | None = None) -> Token: ...

    async def local_login(self) -> Token: ...

    def for_user(self, token: str) -> Api: ...

    async def aclose(self) -> None: ...


M = TypeVar("M", bound=BaseModel)
_vacancies = TypeAdapter(list[VacancyBrief])


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


class _Http:
    def __init__(self, client: httpx.AsyncClient, base: str, token: str | None) -> None:
        self._client = client
        self._base = base
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(
                method, self._base + path, headers=self._headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ApiUnavailable(type(exc).__name__) from exc
        if response.is_error:
            raise _error(response)
        return response

    async def _call(self, model: type[M], method: str, path: str, **kwargs: Any) -> M:
        return model.model_validate((await self._raw(method, path, **kwargs)).json())


class UserClient(_Http):
    async def me(self) -> Me:
        return await self._call(Me, "GET", "/auth/me")

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

    async def vacancies(self, user_id: uuid.UUID) -> list[VacancyBrief]:
        response = await self._raw("GET", f"/users/{user_id}/vacancies")
        return _vacancies.validate_python(response.json())

    async def create_vacancy(
        self, *, text: str | None = None, url: str | None = None, html: str | None = None
    ) -> Vacancy:
        body = {k: v for k, v in {"text": text, "url": url, "html": html}.items() if v is not None}
        return await self._call(Vacancy, "POST", "/vacancies", json=body)

    async def get_vacancy(self, vacancy_id: uuid.UUID) -> Vacancy:
        return await self._call(Vacancy, "GET", f"/vacancies/{vacancy_id}")

    async def set_level(self, vacancy_id: uuid.UUID, level: Level) -> Vacancy:
        return await self._call(Vacancy, "PATCH", f"/vacancies/{vacancy_id}", json={"level": level})

    async def remove_requirement(self, vacancy_id: uuid.UUID, requirement_id: uuid.UUID) -> Vacancy:
        body = {"remove_requirements": [str(requirement_id)]}
        return await self._call(Vacancy, "PATCH", f"/vacancies/{vacancy_id}", json=body)

    async def create_session(
        self,
        vacancy_id: uuid.UUID,
        n: int,
        topics: list[str] | None,
        required_only: bool = False,
        difficulty: Difficulty = "same",
    ) -> Session:
        body: dict[str, Any] = {
            "vacancy_id": str(vacancy_id),
            "n": n,
            "required_only": required_only,
            "difficulty": difficulty,
        }
        if topics:
            body["topics"] = topics
        return await self._call(Session, "POST", "/sessions", json=body)

    async def create_repeat(self, n: int, difficulty: Difficulty = "same") -> Session:
        body = {"mode": "repeat", "n": n, "difficulty": difficulty}
        return await self._call(Session, "POST", "/sessions", json=body)

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
        content_type: str,
    ) -> Accepted:
        path = f"/sessions/{session_id}/questions/{question_id}/answer/voice"
        files = {"file": (filename, data, content_type)}
        return await self._call(Accepted, "POST", path, files=files)

    async def get_answer(self, answer_id: uuid.UUID) -> Answer:
        return await self._call(Answer, "GET", f"/answers/{answer_id}")

    async def review_answer(self, answer_id: uuid.UUID) -> Review:
        """Готовый разбор — сразу, новый — вызов модели, ~20–40 с на Маке (010 §4)."""
        return await self._call(Review, "POST", f"/answers/{answer_id}/review")

    async def get_review(self, answer_id: uuid.UUID) -> Review:
        return await self._call(Review, "GET", f"/answers/{answer_id}/review")

    async def get_progress(self, user_id: uuid.UUID) -> Progress:
        return await self._call(Progress, "GET", f"/users/{user_id}/progress")

    async def forget(self, user_id: uuid.UUID, scope: Literal["audio", "all"]) -> Forgotten:
        return await self._call(
            Forgotten, "DELETE", f"/users/{user_id}/data", params={"scope": scope}
        )

    async def logout(self, *, everywhere: bool = False) -> None:
        await self._raw("DELETE", "/auth/sessions" if everywhere else "/auth/session")


class ApiGate:
    def __init__(
        self,
        base_url: str,
        service_token: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
        self._base = base_url.rstrip("/")
        self._service = _Http(self._client, self._base, service_token)
        self._public = _Http(self._client, self._base, None)

    async def redeem(self, *, secret: str | None = None, code: str | None = None) -> Token:
        body = {"secret": secret} if secret else {"code": code}
        return await self._public._call(Token, "POST", "/auth/links/redeem", json=body)

    async def local_login(self) -> Token:
        return await self._service._call(Token, "POST", "/auth/local")

    def for_user(self, token: str) -> Api:
        return UserClient(self._client, self._base, token)

    async def aclose(self) -> None:
        await self._client.aclose()
