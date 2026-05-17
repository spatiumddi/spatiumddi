"""Appliance k3s cert expiry + kubeapi-expose CIDR list (#183 Phase 6).

Adds two columns:

* ``k3s_api_cert_expires_at`` (timestamp with tz, nullable) — the
  supervisor reports the k3s serving cert's ``Not After`` on every
  heartbeat. Drives the new ``k3s_api_cert_expiring`` alert rule
  at 30 / 7 day thresholds.
* ``kubeapi_expose_cidrs`` (JSONB, default ``[]``) — operator-
  controlled list of CIDRs allowed to reach tcp/6443 directly. The
  supervisor renders one nftables ``ip saddr { ... } tcp dport 6443
  accept`` rule per heartbeat from this list. Empty = proxy-only.

Revision ID: d4c1f7e3b821
Revises: b3a82c0d4e95
Create Date: 2026-05-16 03:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d4c1f7e3b821"
down_revision = "b3a82c0d4e95"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "k3s_api_cert_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "kubeapi_expose_cidrs",
            JSONB,
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "kubeapi_expose_cidrs")
    op.drop_column("appliance", "k3s_api_cert_expires_at")
