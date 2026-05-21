"""appliance evict_requested — dead-node replacement (#272 Phase 9)

Adds ``appliance.evict_requested``: set by the
``/control-plane/{id}/replace`` endpoint on a control-plane member that
died ungracefully. The seed supervisor reads the eviction list off its
heartbeat, deletes the k8s Node (k3s removes the etcd member with it),
and reports the hostname back so the backend clears this flag.

Revision ID: f3b8d24a1c70
Revises: e7a2c91d4f60
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f3b8d24a1c70"
down_revision = "e7a2c91d4f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "evict_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "evict_requested")
