"""Per-server maintenance mode for DNS + DHCP (issue #182).

Three columns mirrored on ``dns_server`` and ``dhcp_server``:

* ``maintenance_mode``: bool, default ``false`` — operator-set
  intent. When true the control plane:
   - skips shipping pending DNSRecordOp / DHCPConfigOp rows
   - auto-resolves heartbeat-stale alerts + suppresses re-fires
   - excludes the server from is_primary cluster-math
* ``maintenance_started_at``: timestamptz, nullable — set when the
  flag flips to true so the UI can render relative duration
  ("Paused 2h ago").
* ``maintenance_reason``: text, nullable — free-text reason captured
  from the operator's Pause confirm modal.

The flag is explicit operator intent — emphatically NOT derived from
"container hasn't checked in for N minutes". That distinction is the
whole point: heartbeat-stale = alert; maintenance = silence.

Revision ID: d7f2a91c4b58
Revises: c4e2b7f81a39
Create Date: 2026-05-15 21:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7f2a91c4b58"
down_revision = "c4e2b7f81a39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("dns_server", "dhcp_server"):
        op.add_column(
            table,
            sa.Column(
                "maintenance_mode",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "maintenance_started_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "maintenance_reason",
                sa.Text(),
                nullable=True,
            ),
        )


def downgrade() -> None:
    for table in ("dns_server", "dhcp_server"):
        op.drop_column(table, "maintenance_reason")
        op.drop_column(table, "maintenance_started_at")
        op.drop_column(table, "maintenance_mode")
