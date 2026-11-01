"""Открытые датасеты: dataset, dataset_item, поля вопроса и ответа, параметры прогона

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02 01:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def upgrade() -> None:
    op.create_table(
        "dataset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("license", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("score_key", sa.Text(), nullable=True),
        sa.Column("score_min", sa.Float(), nullable=True),
        sa.Column("score_max", sa.Float(), nullable=True),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset")),
        sa.UniqueConstraint("name", name=op.f("uq_dataset_name")),
    )
    op.create_table(
        "dataset_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("human_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset.id"],
            name=op.f("fk_dataset_item_dataset_id_dataset"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_item")),
        sa.UniqueConstraint("dataset_id", "external_id", name=op.f("uq_dataset_item_dataset_id")),
    )

    _replace_check("question", "source", "source IN ('generated', 'dataset')")
    op.add_column("question", sa.Column("reference_answer", sa.Text(), nullable=True))
    op.add_column("question", sa.Column("dataset_id", sa.Uuid(), nullable=True))
    op.add_column("question", sa.Column("external_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        op.f("fk_question_dataset_id_dataset"),
        "question",
        "dataset",
        ["dataset_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        op.f("uq_question_dataset_id"), "question", ["dataset_id", "external_id"]
    )

    _replace_check("answer", "source", "source IN ('user', 'dataset')")
    _replace_check(
        "answer",
        "status",
        "status IN ('received', 'transcribing', 'evaluating', 'done', 'failed', 'imported')",
    )
    op.add_column("answer", sa.Column("dataset_item_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_answer_dataset_item_id_dataset_item"),
        "answer",
        "dataset_item",
        ["dataset_item_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(op.f("uq_answer_dataset_item_id"), "answer", ["dataset_item_id"])

    op.add_column(
        "eval_run", sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("eval_run", "params")
    # строки датасетов не влезают в старые CHECK-списки: downgrade возможен только без них
    op.drop_constraint(op.f("uq_answer_dataset_item_id"), "answer", type_="unique")
    op.drop_constraint(op.f("fk_answer_dataset_item_id_dataset_item"), "answer", type_="foreignkey")
    op.drop_column("answer", "dataset_item_id")
    _replace_check(
        "answer",
        "status",
        "status IN ('received', 'transcribing', 'evaluating', 'done', 'failed')",
    )
    _replace_check("answer", "source", "source IN ('user')")
    op.drop_constraint(op.f("uq_question_dataset_id"), "question", type_="unique")
    op.drop_constraint(op.f("fk_question_dataset_id_dataset"), "question", type_="foreignkey")
    op.drop_column("question", "external_id")
    op.drop_column("question", "dataset_id")
    op.drop_column("question", "reference_answer")
    _replace_check("question", "source", "source IN ('generated')")
    op.drop_table("dataset_item")
    op.drop_table("dataset")
