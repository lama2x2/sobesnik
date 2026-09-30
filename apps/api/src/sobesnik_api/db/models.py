"""ORM-модели. Схема — 001-skeleton §7."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sobesnik_api.db.base import Base
from sobesnik_core.vacancy import LEVELS


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


QUESTION_SOURCES = ("generated",)
SESSION_MODES = ("vacancy",)
SESSION_STATUSES = ("active", "finished")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Vacancy(Base):
    __tablename__ = "vacancy"
    __table_args__ = (CheckConstraint(_in("level", LEVELS), name="level"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
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


class Question(Base):
    __tablename__ = "question"
    __table_args__ = (CheckConstraint(_in("source", QUESTION_SOURCES), name="source"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("requirement.id", ondelete="SET NULL"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
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
