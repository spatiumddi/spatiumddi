"""dhcp_server.kea_version — running Kea daemon version

Issue #637. Distinct from ``agent_version`` (the python agent): this is the
version of the Kea daemon itself, read live off the control socket with
``version-get`` and reported on every heartbeat.

The rolling-upgrade preflight (#296) needs it. Kea 3.0's HA hook is
wire-incompatible with peers older than 2.7.0 — 3.0 introduced the "released"
lease state (value 3) in the lease updates partners exchange, and older peers
reject them. So there is no rolling 2.6 → 3.0 upgrade for an HA pair: both
members must cross in the same window. Without this column the orchestrator
cannot tell an operator that *before* they start a node-at-a-time upgrade that
would take their DHCP HA pair down mid-run.

NULL = not reported yet. Consumers must treat that as UNKNOWN, never as "old".

Revision ID: d4a8e2b16f39
Revises: c7d3f9a15e28
Create Date: 2026-07-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a8e2b16f39"
down_revision: str | None = "c7d3f9a15e28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dhcp_server",
        sa.Column("kea_version", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dhcp_server", "kea_version")
