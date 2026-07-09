"""appliance.firewall_state — surface a refused self-partitioning firewall (#593)

The supervisor refuses to apply a firewall drop-in that would close etcd's raft
peer port on a node k3s still labels an etcd member (a stale / diverged
appliance row). Detecting that and only writing a log line would leave the node
silently diverged forever: the control plane keeps re-rendering the same wrong
body from the same row, and nothing tells the operator.

This column carries the refusal to the Fleet UI, on the same route
``port_conflicts`` takes. ``{}`` means healthy.

Revision ID: d1f4b7c92e58
Revises: c7a3f1e28d94
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "d1f4b7c92e58"
down_revision = "c7a3f1e28d94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "firewall_state",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("appliance", "firewall_state")
