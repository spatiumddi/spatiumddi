"""Record the DNS daemon's own version on dns_server (#638)

``dns_server.agent_version`` was never persisted — the heartbeat accepted the
python agent's version and dropped it. What the rolling-upgrade preflight
actually needs is different anyway: the version of the DNS **daemon** the agent
supervises.

PowerDNS 5.0 performs an automatic, silent, irreversible LMDB schema upgrade
(v5 → v6) the first time it opens the database, and pdns 4.9 cannot read the
result. Because ``/var`` is the shared persistent partition on the appliance,
an A/B slot rollback does not undo it. So the #296 rolling-upgrade preflight
has to warn the operator *before* Start when the fleet is still on pdns 4.x —
and to do that it needs to know which major each node is running.

Mirrors ``dhcp_server.kea_version`` from #637, which exists for the same reason
(Kea 3.0's HA hook is wire-incompatible with pre-2.7 peers).

NULL means "not reported": agentless drivers (Windows DNS, the cloud DNS
providers) never report one, and an agent whose probe failed sends None. The
preflight treats NULL as UNKNOWN, never as "old".

Revision ID: f3b6d914c072
Revises: e5a1c39d78b2
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f3b6d914c072"
down_revision: str | None = "e5a1c39d78b2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "dns_server",
        sa.Column("daemon_version", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dns_server", "daemon_version")
