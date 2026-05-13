"""Phase 8f-8 — operator-triggered reboot intent on fleet agent rows

Adds ``reboot_requested`` (bool) + ``reboot_requested_at`` (timestamp)
to ``dns_server`` and ``dhcp_server`` so the Fleet view's per-row
Reboot button can stamp an intent the agent picks up on its next
ConfigBundle long-poll. Mirrors the Phase 8f-4 fleet-upgrade pattern
(stamp on the row → agent reads via ConfigBundle → writes host-side
trigger → systemd path unit reboots the box).

Two columns rather than one so the heartbeat handler can auto-clear
the request once the agent's reconnect timestamp is newer than the
request (proves the reboot actually landed without needing the
agent to send a separate "I rebooted" signal).

Revision ID: a72f4c89e15d
Revises: f8b1c20d3e72
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a72f4c89e15d"
down_revision: str | None = "f8b1c20d3e72"
branch_labels: str | None = None
depends_on: str | None = None


_NEW_COLUMNS = [
    sa.Column(
        "reboot_requested",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    ),
    sa.Column(
        "reboot_requested_at",
        sa.DateTime(timezone=True),
        nullable=True,
    ),
]


def upgrade() -> None:
    for table in ("dns_server", "dhcp_server"):
        for col in _NEW_COLUMNS:
            op.add_column(table, col.copy())


def downgrade() -> None:
    for table in ("dns_server", "dhcp_server"):
        for col in _NEW_COLUMNS:
            op.drop_column(table, col.name)
