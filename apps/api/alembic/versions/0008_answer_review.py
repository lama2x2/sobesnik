"""Подробный разбор ответа: answer_review (010 §4.3)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "answer_review",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("answer_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["answer_id"],
            ["answer.id"],
            name=op.f("fk_answer_review_answer_id_answer"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["evaluation.id"],
            name=op.f("fk_answer_review_evaluation_id_evaluation"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_answer_review")),
        sa.UniqueConstraint("evaluation_id", name=op.f("uq_answer_review_evaluation_id")),
    )
    op.create_index(op.f("ix_answer_review_answer_id"), "answer_review", ["answer_id"])


def downgrade() -> None:
    op.drop_table("answer_review")
