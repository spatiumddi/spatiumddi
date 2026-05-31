"""dhcp scope relay_addresses: per-subnet Kea relay.ip-addresses (giaddr)

Revision ID: a1c4f7e92b30
Revises: d1e7c4a90fb3
Create Date: 2026-05-30 22:00:00.000000

DHCP relay-agent config (issue #337).

Adds ``dhcp_scope.relay_addresses`` — a JSONB list of relay / giaddr IP
addresses. When non-empty the Kea driver renders
``relay: {"ip-addresses": [...]}`` on the scope's ``subnet4`` /
``subnet6`` so a centralized Kea selects the scope for traffic forwarded
by a DHCP relay (``ip helper-address`` / ``dhcrelay``) — required for
subnets that are NOT directly attached to the server. Defaults to an
empty list, preserving today's direct-attach subnet selection for every
existing scope.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1c4f7e92b30"
down_revision: str | None = "d1e7c4a90fb3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "relay_addresses",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dhcp_scope", "relay_addresses")
