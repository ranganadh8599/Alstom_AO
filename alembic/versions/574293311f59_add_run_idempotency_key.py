"""add run idempotency key

Revision ID: 574293311f59
Revises: 89e4790c7052
Create Date: 2026-11-18 00:43:13.035234

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '574293311f59'
down_revision: str | None = '89e4790c7052'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.create_index(op.f("ix_runs_idempotency_key"), "runs", ["idempotency_key"], unique=False)
    # Batch mode so SQLite (dev) and Postgres (production) both apply the
    # constraint; SQLite cannot ADD CONSTRAINT through plain ALTER TABLE.
    with op.batch_alter_table("runs") as batch_op:
        batch_op.create_unique_constraint("uq_runs_user_idempotency", ["user_id", "idempotency_key"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_constraint("uq_runs_user_idempotency", type_="unique")
    op.drop_index(op.f("ix_runs_idempotency_key"), table_name="runs")
    op.drop_column("runs", "idempotency_key")
