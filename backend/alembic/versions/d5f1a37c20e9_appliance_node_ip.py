"""appliance node_ip — real routable host IP (#272 Phase 7b)

Adds ``appliance.node_ip``: the node's k3s-registered InternalIP, as
reported by the supervisor on heartbeat. Distinct from ``last_seen_ip``
(the supervisor POD's source IP, 10.42.x.x, since it heartbeats from
inside the cluster). The control-plane promote endpoint builds the k3s
join URL from the seed's ``node_ip`` — a pod/service IP there is
unreachable by joiners.

Nullable; no backfill — populated on the next heartbeat from each
supervisor.

Revision ID: d5f1a37c20e9
Revises: c263ae1d381a
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d5f1a37c20e9"
down_revision = "c263ae1d381a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appliance", sa.Column("node_ip", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("appliance", "node_ip")
