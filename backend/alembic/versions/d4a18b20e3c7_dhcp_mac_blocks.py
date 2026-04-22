"""DHCP MAC blocklist — group-global deny list.

Revision ID: d4a18b20e3c7
Revises: c7e2f5a91d48
Create Date: 2026-04-22 17:00:00

A blocked MAC address on a ``DHCPServerGroup`` is a row here. Kea
renders the row into the reserved ``DROP`` client class; Windows
DHCP gets ``Add-DhcpServerv4Filter -List Deny`` pushed over WinRM.
Group-global, so a single row blocks the MAC on every scope/subnet
served by the group.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4a18b20e3c7"
down_revision: str | None = "c7e2f5a91d48"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dhcp_mac_block",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "group_id",
            sa.UUID(),
            sa.ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mac_address", postgresql.MACADDR(), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False, server_default="other"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.UUID(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_user_id",
            sa.UUID(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_match_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("match_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("group_id", "mac_address", name="uq_dhcp_mac_block_group_mac"),
    )
    op.create_index("ix_dhcp_mac_block_group", "dhcp_mac_block", ["group_id"])
    op.create_index("ix_dhcp_mac_block_mac", "dhcp_mac_block", ["mac_address"])
    op.create_index("ix_dhcp_mac_block_expires_at", "dhcp_mac_block", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_dhcp_mac_block_expires_at", table_name="dhcp_mac_block")
    op.drop_index("ix_dhcp_mac_block_mac", table_name="dhcp_mac_block")
    op.drop_index("ix_dhcp_mac_block_group", table_name="dhcp_mac_block")
    op.drop_table("dhcp_mac_block")
