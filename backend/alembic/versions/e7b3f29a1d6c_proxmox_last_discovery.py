"""Proxmox — add last_discovery JSONB column.

Revision ID: e7b3f29a1d6c
Revises: e5a72f14c890
Create Date: 2026-04-24 23:00:00

Stores a per-reconcile snapshot of what PVE returned and what we
did with it: counts + a per-guest list with reason codes. Drives
the "Discovery" modal on the Proxmox endpoints screen so operators
can see which VMs aren't reporting runtime IPs (agent off / agent
not installed / no static IP) without reading logs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7b3f29a1d6c"
down_revision: str | None = "e5a72f14c890"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "proxmox_node",
        sa.Column(
            "last_discovery",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("proxmox_node", "last_discovery")
