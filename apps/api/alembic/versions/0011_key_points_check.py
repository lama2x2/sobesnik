"""Проверка ключевых пунктов эталона: question.key_points_check (011 §6.3)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-03 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "question",
        sa.Column("key_points_check", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("question", "key_points_check")
