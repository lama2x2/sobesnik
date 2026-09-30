"""Справочник тем, темы у требований и вопросов, key_points, источник вакансии

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("parent_slug", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["parent_slug"],
            ["topic.slug"],
            name=op.f("fk_topic_parent_slug_topic"),
            onupdate="CASCADE",
        ),
        sa.PrimaryKeyConstraint("slug", name=op.f("pk_topic")),
    )

    for table in ("requirement", "question"):
        op.add_column(table, sa.Column("topic_slug", sa.Text(), nullable=True))
        op.create_index(op.f(f"ix_{table}_topic_slug"), table, ["topic_slug"])
        op.create_foreign_key(
            op.f(f"fk_{table}_topic_slug_topic"),
            table,
            "topic",
            ["topic_slug"],
            ["slug"],
            onupdate="CASCADE",
        )

    op.add_column(
        "question",
        sa.Column(
            "key_points",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )

    op.add_column("vacancy", sa.Column("source", sa.Text(), server_default="text", nullable=False))
    op.add_column("vacancy", sa.Column("source_url", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_vacancy_source"), "vacancy", "source IN ('text', 'url', 'html')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_vacancy_source"), "vacancy", type_="check")
    op.drop_column("vacancy", "source_url")
    op.drop_column("vacancy", "source")
    op.drop_column("question", "key_points")
    for table in ("question", "requirement"):
        op.drop_constraint(op.f(f"fk_{table}_topic_slug_topic"), table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_topic_slug"), table_name=table)
        op.drop_column(table, "topic_slug")
    op.drop_table("topic")
