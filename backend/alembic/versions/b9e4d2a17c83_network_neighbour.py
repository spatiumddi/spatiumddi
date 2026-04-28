"""network_neighbour table + LLDP poll columns on network_device

Revision ID: b9e4d2a17c83
Revises: f4a83cb15920
Create Date: 2026-04-28 23:00:00.000000

Adds the LLDP-MIB lldpRemTable mirror as ``network_neighbour`` plus
two columns on ``network_device``: ``poll_lldp`` (per-device toggle,
mirrors the existing ``poll_arp`` / ``poll_fdb`` / ``poll_interfaces``
flags, defaulted true) and ``last_poll_neighbour_count`` (mirrors the
existing per-table count fields). Vendor-neutral standard MIB —
works on every tier-1 switch without enterprise OIDs.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b9e4d2a17c83"
down_revision: str | None = "f4a83cb15920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "network_device",
        sa.Column(
            "poll_lldp",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "network_device",
        sa.Column("last_poll_neighbour_count", sa.Integer(), nullable=True),
    )
    op.create_table(
        "network_neighbour",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("network_device.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interface_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("network_interface.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("local_port_num", sa.Integer(), nullable=False),
        sa.Column("remote_chassis_id_subtype", sa.Integer(), nullable=False),
        sa.Column("remote_chassis_id", sa.String(length=255), nullable=False),
        sa.Column("remote_port_id_subtype", sa.Integer(), nullable=False),
        sa.Column("remote_port_id", sa.String(length=255), nullable=False),
        sa.Column("remote_port_desc", sa.String(length=255), nullable=True),
        sa.Column("remote_sys_name", sa.String(length=255), nullable=True),
        sa.Column("remote_sys_desc", sa.Text(), nullable=True),
        sa.Column("remote_sys_cap_enabled", sa.Integer(), nullable=True),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "device_id",
            "interface_id",
            "remote_chassis_id",
            "remote_port_id",
            name="uq_network_neighbour_device_iface_remote",
        ),
    )
    op.create_index(
        "ix_network_neighbour_device", "network_neighbour", ["device_id"]
    )
    op.create_index(
        "ix_network_neighbour_remote_sys_name",
        "network_neighbour",
        ["remote_sys_name"],
    )
    op.create_index(
        "ix_network_neighbour_remote_chassis_id",
        "network_neighbour",
        ["remote_chassis_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_network_neighbour_remote_chassis_id", "network_neighbour")
    op.drop_index("ix_network_neighbour_remote_sys_name", "network_neighbour")
    op.drop_index("ix_network_neighbour_device", "network_neighbour")
    op.drop_table("network_neighbour")
    op.drop_column("network_device", "last_poll_neighbour_count")
    op.drop_column("network_device", "poll_lldp")
