"""IPAM — add user_modified_at lock for integration-mirrored rows.

Revision ID: f8d4e29b1c75
Revises: e7b3f29a1d6c
Create Date: 2026-04-24 23:30:00

When an operator edits the soft fields of an integration-mirrored
``ip_address`` row (hostname, description, status, mac_address), the
API write path now stamps this column. Integration reconcilers treat
``user_modified_at IS NOT NULL`` as a lock and skip overwrites of
those four fields — so a VM rename in PVE doesn't blow away the
operator's IPAM name, and a row that pre-existed before the
integration was enabled keeps its operator-chosen values when the
reconciler claims it (sets the integration FK + stamps this column
to lock current values).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f8d4e29b1c75"
down_revision: str | None = "e7b3f29a1d6c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ip_address",
        sa.Column("user_modified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ip_address", "user_modified_at")
