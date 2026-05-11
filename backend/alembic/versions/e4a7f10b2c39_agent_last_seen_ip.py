"""dns_server + dhcp_server.last_seen_ip — surface the agent's real IP

Adds ``last_seen_ip`` to both server tables so the UI can show which
host an agent is actually running on (today only the operator-set
hostname is visible, which is fragile in distributed deployments).
Populated from ``request.client.host`` on every agent heartbeat —
captures the public-side IP in NAT scenarios, which is what operators
need to triage "which box is dns1?".

Revision ID: e4a7f10b2c39
Revises: d8f3a92e0c47
Create Date: 2026-05-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e4a7f10b2c39"
down_revision: str | None = "d8f3a92e0c47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 45 chars covers IPv6's max textual length (39) plus a zone-id
    # suffix like %eth0 with room to spare; tighter than the more
    # common VARCHAR(255) "address" columns.
    op.add_column(
        "dns_server",
        sa.Column("last_seen_ip", sa.String(length=45), nullable=True),
    )
    op.add_column(
        "dhcp_server",
        sa.Column("last_seen_ip", sa.String(length=45), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dhcp_server", "last_seen_ip")
    op.drop_column("dns_server", "last_seen_ip")
