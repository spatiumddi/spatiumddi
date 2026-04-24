"""Proxmox — add infer_vnet_subnets toggle.

Revision ID: e5a72f14c890
Revises: d1a8f3c704e9
Create Date: 2026-04-24 22:00:00

Adds the ``infer_vnet_subnets`` column to ``proxmox_node``. When on,
the reconciler fills in missing SDN VNet CIDRs by inspecting the
guests attached to each VNet — useful for deployments where PVE is
pure L2 passthrough and the IP plan lives on an upstream router.
Default False keeps existing endpoints behaving as before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e5a72f14c890"
down_revision: str | None = "d1a8f3c704e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "proxmox_node",
        sa.Column(
            "infer_vnet_subnets",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("proxmox_node", "infer_vnet_subnets")
