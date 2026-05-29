"""dhcp scope — DHCPv6 address mode + RA flags (#52)

Revision ID: e4c1a8f63b29
Revises: d7a3f2b9c1e4
Create Date: 2026-05-29 20:30:00.000000

Adds the DHCPv6 operating-mode discriminator to ``dhcp_scope`` (issue
#52). ``v6_address_mode`` is only meaningful for ipv6 scopes — it drives
how Kea renders the subnet6 (stateful = address pools; stateless =
options only; slaac = no DHCP address service). ``ra_managed_flag`` /
``ra_other_flag`` record the intended Router-Advertisement M/O flags
(operator applies these on their router; SpatiumDDI's Kea agent doesn't
send RAs). Additive only — every column ships a server_default so
existing v4 + v6 scopes backfill to "stateful" (current behaviour).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4c1a8f63b29"
down_revision: Union[str, None] = "d7a3f2b9c1e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "v6_address_mode",
            sa.String(length=12),
            nullable=False,
            server_default="stateful",
        ),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "ra_managed_flag",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "dhcp_scope",
        sa.Column(
            "ra_other_flag",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dhcp_scope", "ra_other_flag")
    op.drop_column("dhcp_scope", "ra_managed_flag")
    op.drop_column("dhcp_scope", "v6_address_mode")
