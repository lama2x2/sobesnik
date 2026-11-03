"""Интервальное повторение SM-2: срок повторения темы (011 §5.4)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-03 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # до пересчёта (`sobesnik-cli progress rebuild` или следующая оценка) срок пустой = «сейчас»
    op.add_column("topic_progress", sa.Column("reps", sa.Integer(), nullable=True))
    op.add_column("topic_progress", sa.Column("ease", sa.Float(), nullable=True))
    op.add_column("topic_progress", sa.Column("interval_days", sa.Integer(), nullable=True))
    op.add_column("topic_progress", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("topic_progress", "due_at")
    op.drop_column("topic_progress", "interval_days")
    op.drop_column("topic_progress", "ease")
    op.drop_column("topic_progress", "reps")
