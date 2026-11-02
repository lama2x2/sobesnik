"""Схемы запросов и ответов API (001 §8, 002 §11, 003 §10, 007 §7)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

from sobesnik_core.limits import (
    ANSWER_MAX_CHARS,
    MAX_VACANCY_CHARS,
    QUESTIONS_DEFAULT,
    QUESTIONS_MAX,
    QUESTIONS_MIN,
)
from sobesnik_core.topics import topic_title
from sobesnik_core.vacancy import Level

MAX_HTML_CHARS = 3 * 1024 * 1024
MAX_URL_CHARS = 2_000


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    telegram_id: int | None
    created_at: datetime


class VacancyCreate(BaseModel):
    """Ровно одно из полей: text, url, html."""

    user_id: uuid.UUID
    text: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_VACANCY_CHARS),
        ]
        | None
    ) = None
    url: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_URL_CHARS)
        ]
        | None
    ) = None
    html: Annotated[str, StringConstraints(min_length=1, max_length=MAX_HTML_CHARS)] | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> Self:
        given = [f for f in ("text", "url", "html") if getattr(self, f) is not None]
        if len(given) != 1:
            raise ValueError("нужно ровно одно из полей: text, url, html")
        return self


class VacancyPatch(BaseModel):
    level: Level | None = None
    remove_requirements: list[uuid.UUID] | None = None

    @model_validator(mode="after")
    def not_empty(self) -> Self:
        if self.level is None and not self.remove_requirements:
            raise ValueError("нечего менять: укажите level или remove_requirements")
        return self


class RequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    text: str
    is_required: bool
    topic: str | None = Field(validation_alias="topic_slug")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def topic_title(self) -> str | None:
        """Заголовок темы — для кнопок выбора тем в боте (007 §8.3)."""
        return topic_title(self.topic)


class VacancyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    level: Level
    stack: list[str]
    requirements: list[RequirementOut]
    source: str
    source_url: str | None
    parser_model: str
    prompt_version: str
    created_at: datetime


class SessionCreate(BaseModel):
    """`{vacancy_id, n, topics?}` — по вакансии; `{mode: "repeat", user_id, n}` (007 §6)."""

    mode: Literal["vacancy", "repeat"] = "vacancy"
    vacancy_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    n: int = Field(default=QUESTIONS_DEFAULT, ge=QUESTIONS_MIN, le=QUESTIONS_MAX)
    topics: list[str] | None = Field(default=None, min_length=1, max_length=QUESTIONS_MAX)

    @model_validator(mode="after")
    def by_mode(self) -> Self:
        if self.mode == "vacancy" and self.vacancy_id is None:
            raise ValueError("для сессии по вакансии нужен vacancy_id")
        if self.mode == "repeat" and (self.user_id is None or self.topics is not None):
            raise ValueError("для повторения нужен user_id, а topics не задаются")
        return self


class SessionAnswerOut(BaseModel):
    """Последний ответ на вопрос сессии."""

    id: uuid.UUID
    kind: str
    status: str
    total: int | None
    passed: bool | None


class SessionQuestionOut(BaseModel):
    id: uuid.UUID
    position: int
    text: str
    requirement_id: uuid.UUID | None
    topic: str | None
    key_points: list[str]
    answer: SessionAnswerOut | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    vacancy_id: uuid.UUID | None
    mode: str
    topics: list[str] | None = None
    status: str
    questions: list[SessionQuestionOut]


class AnswerCreate(BaseModel):
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=ANSWER_MAX_CHARS)
    ]


class AnswerAccepted(BaseModel):
    answer_id: uuid.UUID
    status: str


class CriterionScoreOut(BaseModel):
    criterion: str
    title: str
    weight: float
    score: int
    reason: str


class EvaluationOut(BaseModel):
    id: uuid.UUID
    total: int
    passed: bool
    scores: list[CriterionScoreOut]
    missed_points: list[str]
    advice: str
    asr_doubts: list[str]
    """Места, где мог ошибиться распознаватель речи (004 §8.6)."""
    rubric_version: str
    prompt_version: str
    model: str
    latency_ms: int
    created_at: datetime


class AudioOut(BaseModel):
    mime: str
    duration_s: float
    expires_at: datetime
    deleted: bool


class TranscriptOut(BaseModel):
    id: uuid.UUID
    kind: str
    text: str
    language: str
    asr_model: str | None
    latency_ms: int | None


class AnswerOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID | None
    question_id: uuid.UUID
    kind: str
    text: str | None
    status: str
    error: str | None
    created_at: datetime
    audio: AudioOut | None = None
    transcript: TranscriptOut | None = None
    evaluation: EvaluationOut | None
    topic: str | None = None
    docs: list["TopicDocOut"] = []
    """До двух ссылок «что почитать» по теме вопроса (007 §7.2)."""


class TopicDocOut(BaseModel):
    title: str
    url: str


class ForgetOut(BaseModel):
    audio_deleted: int
    transcripts_deleted: int
    answers_deleted: int | None = None
    sessions_deleted: int | None = None
    vacancies_deleted: int | None = None
    """Поля `*_deleted` кроме аудио и расшифровок — только при `scope=all` (007 §7.3)."""


class ThresholdsOut(BaseModel):
    min_attempts: int
    weak_avg: float
    low_score: float


class TopicProgressOut(BaseModel):
    slug: str
    title: str
    status: Literal["weak", "few", "ok"]
    low: bool
    """«Мало данных», но балл низкий — «похоже, стоит повторить»."""
    attempts: int
    last_score: int | None
    avg_last3: float | None
    docs: list[TopicDocOut]


class SessionHistoryOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    mode: str
    vacancy_title: str | None
    answered: int
    passed: int
    avg_total: float | None
    scores: list[int | None]


class ProgressOut(BaseModel):
    thresholds: ThresholdsOut
    topics: list[TopicProgressOut]
    sessions: list[SessionHistoryOut]


class SummaryQuestionOut(BaseModel):
    position: int
    question_id: uuid.UUID
    answer_status: str | None
    total: int | None
    passed: bool | None


class SessionSummaryOut(BaseModel):
    id: uuid.UUID
    status: str
    finished_at: datetime | None
    questions: list[SummaryQuestionOut]
    answered: int
    evaluated: int
    passed: int
    avg_total: float | None


class DatasetOut(BaseModel):
    """Импортированный датасет (006 §5.2) — как `sobesnik-cli dataset list`."""

    name: str
    lang: str
    license: str
    source_url: str | None
    answers: int
    voice: int
    transcribed: int
    imported_at: datetime


class DatasetImportOut(BaseModel):
    dataset: str
    questions: int
    answers: int


class EvalRunCreate(BaseModel):
    """Прогон (006 §5.2) — те же параметры, что у `sobesnik-cli eval-run create`."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    model: str | None = None
    """По умолчанию — модель роли eval."""
    dataset: str | None = None
    session_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    since: date | None = None
    until: date | None = None
    rubric: int | None = None
    prompt: int | None = None
    temperature: float | None = None
    seed: int | None = None
    transcript: Literal["asr", "reference"] | None = None


class EvalRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    provider: str
    model: str
    rubric_version: str
    prompt_version: str
    transcript_kind: str | None
    filter: dict[str, Any]
    params: dict[str, Any] | None
    status: str
    total: int
    done: int
    failed: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None
