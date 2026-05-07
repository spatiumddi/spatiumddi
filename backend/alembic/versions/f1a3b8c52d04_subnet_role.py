"""Subnet network-role classification (issue #112 phase 2).

Revision ID: f1a3b8c52d04
Revises: e4d8c2a91f7b
Create Date: 2026-05-07 18:00:00

Adds ``subnet.subnet_role`` (nullable VARCHAR(20)) — the
``data | voice | management | guest`` enum from issue #112 phase 2.
NULL = unspecified, so existing rows aren't retroactively
misclassified. Indexed partial-style: a single index over the
column with NULL excluded so role-filter queries hit it without
the NULL bucket dragging cardinality.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "f1a3b8c52d04"
down_revision: str | None = "e4d8c2a91f7b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subnet", sa.Column("subnet_role", sa.String(20), nullable=True))
    op.create_index(
        "ix_subnet_subnet_role",
        "subnet",
        ["subnet_role"],
        postgresql_where=sa.text("subnet_role IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_subnet_subnet_role", table_name="subnet")
    op.drop_column("subnet", "subnet_role")
