"""dhcp scope: dns_track_dynamic_leases opt-out for lease-mirror DNS drift

Adds ``dhcp_scope.dns_track_dynamic_leases`` (default True). When False, the
IPAM↔DNS drift check ignores the scope's dynamic-pool lease mirrors
(``auto_from_lease`` IPs inside a ``dynamic`` pool) so ephemeral pulled leases
without DNS records don't read as "out of sync". Default True preserves prior
behaviour.

Revision ID: c9a1f4e07b52
Revises: b3e7d21c9f04
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c9a1f4e07b52"
down_revision = "b3e7d21c9f04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "dns_track_dynamic_leases",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dhcp_scope", "dns_track_dynamic_leases")
