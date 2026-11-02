"""Прогресс по темам: topic_progress, сессии повторения и выбранные темы

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def upgrade() -> None:
    op.create_table(
        "topic_progress",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("topic_slug", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_scores", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("last_score", sa.Integer(), nullable=False),
        sa.Column("avg_last3", sa.Float(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_topic_progress_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_slug"],
            ["topic.slug"],
            name=op.f("fk_topic_progress_topic_slug_topic"),
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "topic_slug", name=op.f("pk_topic_progress")),
    )
    op.add_column("session", sa.Column("topics", postgresql.ARRAY(sa.Text()), nullable=True))
    _replace_check("session", "mode", "mode IN ('vacancy', 'repeat')")


def downgrade() -> None:
    op.execute("DELETE FROM session WHERE mode = 'repeat'")
    _replace_check("session", "mode", "mode IN ('vacancy')")
    op.drop_column("session", "topics")
    op.drop_table("topic_progress")
