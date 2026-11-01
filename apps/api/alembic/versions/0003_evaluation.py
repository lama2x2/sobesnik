"""Ответы, оценки и прогоны оценки

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "answer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('user')", name=op.f("ck_answer_source")),
        sa.CheckConstraint("kind IN ('text')", name=op.f("ck_answer_kind")),
        sa.CheckConstraint(
            "status IN ('received', 'evaluating', 'done', 'failed')",
            name=op.f("ck_answer_status"),
        ),
        sa.ForeignKeyConstraint(
            ["question_id"], ["question.id"], name=op.f("fk_answer_question_id_question")
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session.id"],
            name=op.f("fk_answer_session_id_session"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_answer_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_answer")),
    )
    op.create_index(op.f("ix_answer_question_id"), "answer", ["question_id"])
    op.create_index(op.f("ix_answer_user_id"), "answer", ["user_id"])
    op.create_index("ix_answer_session_id_question_id", "answer", ["session_id", "question_id"])

    op.create_table(
        "eval_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("rubric_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column(
            "filter",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("done", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name=op.f("ck_eval_run_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_run")),
    )

    op.create_table(
        "evaluation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("answer_id", sa.Uuid(), nullable=False),
        sa.Column("eval_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rubric_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scores", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("feedback", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint("status IN ('done', 'failed')", name=op.f("ck_evaluation_status")),
        sa.ForeignKeyConstraint(
            ["answer_id"],
            ["answer.id"],
            name=op.f("fk_evaluation_answer_id_answer"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["eval_run_id"],
            ["eval_run.id"],
            name=op.f("fk_evaluation_eval_run_id_eval_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation")),
    )
    op.create_index(op.f("ix_evaluation_answer_id"), "evaluation", ["answer_id"])
    op.create_index(op.f("ix_evaluation_eval_run_id"), "evaluation", ["eval_run_id"])


def downgrade() -> None:
    op.drop_table("evaluation")
    op.drop_table("eval_run")
    op.drop_table("answer")
