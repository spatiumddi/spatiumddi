"""dhcp server group: add dhcp_socket_mode (issue #365)

Adds ``dhcp_server_group.dhcp_socket_mode`` — the Kea ``dhcp-socket-type``
selector carried on the group (a per-daemon Kea setting, so it can't vary
per subnet):

  * ``direct`` → ``raw`` sockets (default). Receives broadcast DISCOVERs
    from directly-attached clients that have no IP yet *and* relayed
    traffic. Kea's own default.
  * ``relay`` → ``udp`` sockets. Relay-only; cannot receive direct L2
    broadcasts.

Existing rows backfill to ``direct`` via the column ``server_default`` so
upgraded installs switch from the old hardcoded ``udp`` render to ``raw``
and start answering directly-attached clients with no operator action.

Revision ID: f3b9c1d6a274
Revises: b6f4d2a91c83
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f3b9c1d6a274"
down_revision: str | None = "b6f4d2a91c83"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # NOT NULL with a server_default → Postgres backfills every existing
    # row to "direct" as part of the ADD COLUMN, so no separate UPDATE is
    # needed. The model carries the same server_default.
    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "dhcp_socket_mode",
            sa.String(length=16),
            nullable=False,
            server_default="direct",
        ),
    )


def downgrade() -> None:
    op.drop_column("dhcp_server_group", "dhcp_socket_mode")
