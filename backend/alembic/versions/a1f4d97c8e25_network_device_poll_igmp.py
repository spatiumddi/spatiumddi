"""network_device.poll_igmp_snooping toggle — issue #126 Phase 3 Wave 1

Revision ID: a1f4d97c8e25
Revises: d3a9c5b71e84
Create Date: 2026-05-09 02:00:00

Adds the IGMP-snooping populator opt-in column to NetworkDevice
plus a ``last_poll_igmp_count`` counter alongside the existing
``last_poll_arp_count`` / ``last_poll_fdb_count`` / etc.

Default-off because the populator surfaces transient join/leave
churn that fills ``MulticastMembership`` with rows that come and
go on every poll. Operators flip the toggle on per-device once
they're committed to tracking consumers via SNMP discovery.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1f4d97c8e25"
down_revision: str | None = "d3a9c5b71e84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "network_device",
        sa.Column(
            "poll_igmp_snooping",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "network_device",
        sa.Column("last_poll_igmp_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("network_device", "last_poll_igmp_count")
    op.drop_column("network_device", "poll_igmp_snooping")
