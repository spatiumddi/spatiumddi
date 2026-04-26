"""Subnet split / merge ops + IPSpace VRF annotation columns.

Revision ID: f1c8b2a945d3
Revises: f1c9a4d2b8e6

Create Date: 2026-04-26 14:00:00

This migration is purely additive on the IPSpace table. It introduces the
three VRF / routing annotation columns described in
``app.models.ipam.IPSpace``:

* ``vrf_name`` (VARCHAR(64), nullable) — canonical VRF name.
* ``route_distinguisher`` (VARCHAR(32), nullable) — RD in either ASN:idx
  or IPv4:idx form (no validation; vendors disagree).
* ``route_targets`` (JSONB, nullable) — list of RT strings; ``[]`` is a
  legal value distinct from NULL.

No semantic changes to address allocation or any other behaviour: VRF
overlap is already provided by separate IPSpace rows, and these columns
are pure annotation. Backfill is therefore a no-op (existing rows get
NULL for all three).

The split / merge endpoints introduced alongside this migration do not
change the schema — they reuse the existing ``Subnet`` / ``IPAddress``
/ ``DHCPScope`` / ``DNSRecord`` tables. So this migration only carries
the IPSpace columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f1c8b2a945d3"
down_revision: str | None = "f1c9a4d2b8e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ip_space",
        sa.Column("vrf_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ip_space",
        sa.Column("route_distinguisher", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "ip_space",
        sa.Column("route_targets", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ip_space", "route_targets")
    op.drop_column("ip_space", "route_distinguisher")
    op.drop_column("ip_space", "vrf_name")
