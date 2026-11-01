"""Голосовой ответ: аудио, транскрипты, ссылка оценки на транскрипт

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def upgrade() -> None:
    _replace_check("answer", "kind", "kind IN ('text', 'voice')")
    _replace_check(
        "answer",
        "status",
        "status IN ('received', 'transcribing', 'evaluating', 'done', 'failed')",
    )
    op.create_table(
        "audio",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("answer_id", sa.Uuid(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["answer_id"],
            ["answer.id"],
            name=op.f("fk_audio_answer_id_answer"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audio")),
        sa.UniqueConstraint("answer_id", name=op.f("uq_audio_answer_id")),
    )
    op.create_index(
        "ix_audio_expires_at",
        "audio",
        ["expires_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "transcript",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("audio_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("language_prob", sa.Float(), nullable=True),
        sa.Column("asr_model", sa.Text(), nullable=True),
        sa.Column("avg_logprob", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _created_at(),
        sa.CheckConstraint("kind IN ('asr', 'reference')", name=op.f("ck_transcript_kind")),
        sa.ForeignKeyConstraint(
            ["audio_id"],
            ["audio.id"],
            name=op.f("fk_transcript_audio_id_audio"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcript")),
    )
    op.create_index("ix_transcript_audio_id_kind", "transcript", ["audio_id", "kind"])
    op.add_column("evaluation", sa.Column("transcript_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_evaluation_transcript_id_transcript"),
        "evaluation",
        "transcript",
        ["transcript_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_evaluation_transcript_id"), "evaluation", ["transcript_id"])
    op.add_column("eval_run", sa.Column("transcript_kind", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_eval_run_transcript_kind"),
        "eval_run",
        "transcript_kind IN ('asr', 'reference')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_eval_run_transcript_kind"), "eval_run", type_="check")
    op.drop_column("eval_run", "transcript_kind")
    op.drop_index(op.f("ix_evaluation_transcript_id"), table_name="evaluation")
    op.drop_constraint(
        op.f("fk_evaluation_transcript_id_transcript"), "evaluation", type_="foreignkey"
    )
    op.drop_column("evaluation", "transcript_id")
    op.drop_table("transcript")
    op.drop_table("audio")
    # голосовые ответы не влезают в старые CHECK-списки: downgrade возможен только без них
    _replace_check("answer", "status", "status IN ('received', 'evaluating', 'done', 'failed')")
    _replace_check("answer", "kind", "kind IN ('text')")
