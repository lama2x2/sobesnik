"""ORM-модели. Схема — 001-skeleton §7, 002 §10, 003 §7, 004 §7."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sobesnik_api.db.base import Base
from sobesnik_core.vacancy import LEVELS


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


QUESTION_SOURCES = ("generated",)
VACANCY_SOURCES = ("text", "url", "html")
SESSION_MODES = ("vacancy",)
SESSION_STATUSES = ("active", "finished")
ANSWER_SOURCES = ("user",)
ANSWER_KINDS = ("text", "voice")
ANSWER_STATUSES = ("received", "transcribing", "evaluating", "done", "failed")
TRANSCRIPT_KINDS = ("asr", "reference")
EVALUATION_STATUSES = ("done", "failed")
EVAL_RUN_STATUSES = ("queued", "running", "done", "failed")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Topic(Base):
    """Справочник тем. Источник правды — topics.yaml в sobesnik_core, таблица синхронизируется."""

    __tablename__ = "topic"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    parent_slug: Mapped[str | None] = mapped_column(ForeignKey("topic.slug", onupdate="CASCADE"))


def _topic_fk() -> ForeignKey:
    return ForeignKey("topic.slug", onupdate="CASCADE")


class Vacancy(Base):
    __tablename__ = "vacancy"
    __table_args__ = (
        CheckConstraint(_in("level", LEVELS), name="level"),
        CheckConstraint(_in("source", VACANCY_SOURCES), name="source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="text", server_default="text")
    source_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text)
    stack: Mapped[list[str]] = mapped_column(ARRAY(Text))
    parser_model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    requirements: Mapped[list["Requirement"]] = relationship(
        order_by="Requirement.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class Requirement(Base):
    __tablename__ = "requirement"
    __table_args__ = (UniqueConstraint("vacancy_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    vacancy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vacancy.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(Boolean)
    topic_slug: Mapped[str | None] = mapped_column(_topic_fk(), index=True)


class Question(Base):
    __tablename__ = "question"
    __table_args__ = (CheckConstraint(_in("source", QUESTION_SOURCES), name="source"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("requirement.id", ondelete="SET NULL"), index=True
    )
    topic_slug: Mapped[str | None] = mapped_column(_topic_fk(), index=True)
    text: Mapped[str] = mapped_column(Text)
    key_points: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb")
    )
    source: Mapped[str] = mapped_column(Text, default="generated")
    gen_model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrainingSession(Base):
    """Тренировка. Класс назван так, чтобы не путать с сессией SQLAlchemy."""

    __tablename__ = "session"
    __table_args__ = (
        CheckConstraint(_in("mode", SESSION_MODES), name="mode"),
        CheckConstraint(_in("status", SESSION_STATUSES), name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    vacancy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vacancy.id"))
    mode: Mapped[str] = mapped_column(Text, default="vacancy")
    status: Mapped[str] = mapped_column(Text, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["SessionQuestion"]] = relationship(
        order_by="SessionQuestion.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class SessionQuestion(Base):
    __tablename__ = "session_question"
    __table_args__ = (
        PrimaryKeyConstraint("session_id", "position"),
        UniqueConstraint("session_id", "question_id"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id", ondelete="CASCADE"))
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("question.id"))
    position: Mapped[int] = mapped_column(Integer)

    question: Mapped[Question] = relationship(lazy="selectin")


class Answer(Base):
    __tablename__ = "answer"
    __table_args__ = (
        CheckConstraint(_in("source", ANSWER_SOURCES), name="source"),
        CheckConstraint(_in("kind", ANSWER_KINDS), name="kind"),
        CheckConstraint(_in("status", ANSWER_STATUSES), name="status"),
        Index("ix_answer_session_id_question_id", "session_id", "question_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("question.id"), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(Text, default="user")
    kind: Mapped[str] = mapped_column(Text, default="text")
    text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="received")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    question: Mapped[Question] = relationship(lazy="selectin")


class Audio(Base):
    """Аудио голосового ответа. Сам файл — в хранилище по `s3_key` (ASR-1).

    По TTL удаляется объект в хранилище, а строка остаётся с `deleted_at`: на неё ссылаются
    транскрипты.
    """

    __tablename__ = "audio"
    __table_args__ = (
        Index(
            "ix_audio_expires_at",
            "expires_at",
            postgresql_where=sql_text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("answer.id", ondelete="CASCADE"), unique=True
    )
    s3_key: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    duration_s: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transcript(Base):
    """Транскрипт аудио: распознанный (`asr`) или ручная расшифровка (`reference`), ASR-3."""

    __tablename__ = "transcript"
    __table_args__ = (
        CheckConstraint(_in("kind", TRANSCRIPT_KINDS), name="kind"),
        Index("ix_transcript_audio_id_kind", "audio_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    audio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("audio.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    language_prob: Mapped[float | None] = mapped_column(Float)
    asr_model: Mapped[str | None] = mapped_column(Text)
    avg_logprob: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalRun(Base):
    """Прогон: переоценка набора ответов заданной моделью (EVAL-6)."""

    __tablename__ = "eval_run"
    __table_args__ = (
        CheckConstraint(_in("status", EVAL_RUN_STATUSES), name="status"),
        CheckConstraint(_in("transcript_kind", TRANSCRIPT_KINDS), name="transcript_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    rubric_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    transcript_kind: Mapped[str | None] = mapped_column(Text)
    """Какой транскрипт оценивать у голосовых ответов; None — как в бою (004 §9.1)."""
    filter: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, default="queued")
    total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    done: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evaluation(Base):
    """Оценка ответа. Не изменяется: переоценка добавляет строку (EVAL-4)."""

    __tablename__ = "evaluation"
    __table_args__ = (CheckConstraint(_in("status", EVALUATION_STATUSES), name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("answer.id", ondelete="CASCADE"), index=True
    )
    eval_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("eval_run.id", ondelete="CASCADE"), index=True
    )
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transcript.id", ondelete="SET NULL"), index=True
    )
    """Какой транскрипт оценён (EVAL-5); у текстового ответа — None."""
    status: Mapped[str] = mapped_column(Text)
    rubric_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    scores: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """{criterion: {"score": 0–3, "reason": "…"}}."""
    total: Mapped[int | None] = mapped_column(Integer)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    feedback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """{"missed_points": [...], "wrong_points": [...], "advice": "…", "asr_doubts": [...]}."""
    raw_response: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
