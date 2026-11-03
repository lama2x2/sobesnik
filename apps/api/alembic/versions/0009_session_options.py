"""Настройки тренировки: только обязательные требования и сложность (010 §5.3)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-03 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session",
        sa.Column("required_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "session", sa.Column("difficulty", sa.Text(), server_default="same", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_session_difficulty"), "session", "difficulty IN ('easier', 'same', 'harder')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_session_difficulty"), "session", type_="check")
    op.drop_column("session", "difficulty")
    op.drop_column("session", "required_only")
