"""Время модели на разбор вакансии и генерацию вопросов — для сводки времени (011 §7)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-04 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("vacancy", sa.Column("latency_ms", sa.Integer(), nullable=True))
    op.add_column("session", sa.Column("gen_latency_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("session", "gen_latency_ms")
    op.drop_column("vacancy", "latency_ms")
