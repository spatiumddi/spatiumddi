"""platform_settings MetalLB control-plane VIP (#272 Phase 7c)

Adds the cluster-wide MetalLB / control-plane-VIP knobs to the
``platform_settings`` singleton:

* ``metallb_enabled`` — master switch for the bundled MetalLB.
* ``metallb_pool_addresses`` — JSONB list of L2 pool entries (CIDR or
  ``a.b.c.10-a.b.c.20`` range).
* ``control_plane_vip`` — the single floating IP fronting the frontend
  Service. Must fall inside the pool.

The seed supervisor reads these back on heartbeat and patches the
spatium-bootstrap (``metallb.*``) + spatium-control
(``frontend.controlPlaneVIP``) HelmCharts. Defaults keep a fresh
single-node install on the hostNetwork frontend (no VIP).

Revision ID: e7a2c91d4f60
Revises: d5f1a37c20e9
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "e7a2c91d4f60"
down_revision = "d5f1a37c20e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "metallb_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "metallb_pool_addresses",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "control_plane_vip",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "control_plane_vip")
    op.drop_column("platform_settings", "metallb_pool_addresses")
    op.drop_column("platform_settings", "metallb_enabled")
