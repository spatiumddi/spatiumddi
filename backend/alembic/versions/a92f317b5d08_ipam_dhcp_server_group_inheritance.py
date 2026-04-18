"""IPAM → DHCP server group inheritance.

Adds ``dhcp_server_group_id`` to ``ip_space``, ``ip_block``, ``subnet`` and
``dhcp_inherit_settings`` to the latter two — mirroring the existing DNS
inheritance fields so a scope can pick up its server group from the enclosing
IPAM hierarchy instead of being set ad-hoc at scope creation time.

Revision ID: a92f317b5d08
Revises: c4e8f1a25d93
Create Date: 2026-04-17 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a92f317b5d08"
down_revision: str | None = "c4e8f1a25d93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("ip_space", "ip_block", "subnet"):
        op.add_column(
            table,
            sa.Column(
                "dhcp_server_group_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("dhcp_server_group.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )

    for table in ("ip_block", "subnet"):
        op.add_column(
            table,
            sa.Column(
                "dhcp_inherit_settings",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def downgrade() -> None:
    for table in ("ip_block", "subnet"):
        op.drop_column(table, "dhcp_inherit_settings")
    for table in ("ip_space", "ip_block", "subnet"):
        op.drop_column(table, "dhcp_server_group_id")
