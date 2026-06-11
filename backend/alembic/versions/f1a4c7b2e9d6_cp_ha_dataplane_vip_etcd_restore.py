"""Control-plane HA: data-plane VIPs (#272 Phase 10) + etcd restore (#272 Phase 9b)

Phase 10 — adds ``platform_settings.dns_vip`` + ``dhcp_relay_vip`` (the
cluster-wide data-plane resolver VIPs, same MetalLB pool as the
control-plane VIP). Phase 9b — adds ``appliance.etcd_snapshots`` (the
seed-reported snapshot inventory), ``desired_restore_snapshot`` (guided
restore desired-state), and ``restore_state`` / ``restore_reason``
(runner-reported progress).

All additive + nullable / defaulted, so existing rows backfill to the
single-node defaults (no VIP, no snapshots, no restore in flight).

Revision ID: f1a4c7b2e9d6
Revises: e7a3c0d519f4
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f1a4c7b2e9d6"
down_revision = "e7a3c0d519f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── #272 Phase 10 — data-plane resolver VIPs (platform_settings) ──
    op.add_column(
        "platform_settings",
        sa.Column(
            "dns_vip", sa.String(length=64), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dhcp_relay_vip", sa.String(length=64), nullable=False, server_default=""
        ),
    )

    # ── #272 Phase 9b — etcd snapshot inventory + guided restore (appliance) ──
    op.add_column(
        "appliance",
        sa.Column(
            "etcd_snapshots",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "appliance",
        sa.Column("desired_restore_snapshot", sa.Text(), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("restore_state", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("restore_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "restore_reason")
    op.drop_column("appliance", "restore_state")
    op.drop_column("appliance", "desired_restore_snapshot")
    op.drop_column("appliance", "etcd_snapshots")
    op.drop_column("platform_settings", "dhcp_relay_vip")
    op.drop_column("platform_settings", "dns_vip")
