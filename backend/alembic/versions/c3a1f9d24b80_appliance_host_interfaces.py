"""appliance.host_interfaces — host NICs for the appliance-vantage pcap picker (#59)

The supervisor enumerates the appliance host's real NICs (from
/run/udev/data) and reports them on every heartbeat; the packet-capture
appliance vantage surfaces them as an interface dropdown so operators
pick a NIC (e.g. ens18) instead of guessing or being told it's a
"follow-up phase".

Revision ID: c3a1f9d24b80
Revises: b8e3f1c47a92
Create Date: 2026-06-15

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3a1f9d24b80"
down_revision = "b8e3f1c47a92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column("host_interfaces", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "host_interfaces")
