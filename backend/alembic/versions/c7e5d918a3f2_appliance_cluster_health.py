"""Appliance cluster_health JSONB column (#183 Phase 4).

Adds a single column on the ``appliance`` table that carries the
supervisor's local k3s health summary (``kubeapi_ready``,
``nodes_total``, ``nodes_ready``, ``pods_total``, ``pods_by_phase``).

The supervisor's heartbeat probes the local kubeapi once per tick
and overwrites the column verbatim. Empty dict means "no probe ran"
or "legacy compose deployment". The Fleet UI reads the field for a
cluster-health row on the appliance drilldown.

Revision ID: c7e5d918a3f2
Revises: a91f72c4b1d8
Create Date: 2026-05-16 02:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c7e5d918a3f2"
down_revision = "a91f72c4b1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "cluster_health",
            JSONB,
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "cluster_health")
