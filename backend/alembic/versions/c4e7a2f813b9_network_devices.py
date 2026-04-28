"""Network discovery — SNMP-polled devices + ARP / FDB / interface tables.

Revision ID: c4e7a2f813b9
Revises: f5b9c1e8d472
Create Date: 2026-04-27 12:00:00

Creates the four tables backing the new Network Discovery feature:

  * ``network_device`` — operator-registered switches / routers / APs
    polled via standard SNMP MIBs. Carries Fernet-encrypted v1/v2c
    community + v3 USM auth/priv keys.
  * ``network_interface`` — IF-MIB ifTable rows per device.
  * ``network_arp_entry`` — IP-MIB ipNetToPhysicalTable rows
    (with RFC1213-MIB ipNetToMediaTable fallback).
  * ``network_fdb_entry`` — Q-BRIDGE-MIB dot1qTpFdbTable rows
    (with BRIDGE-MIB dot1dTpFdbTable fallback). The
    ``(device, mac, vlan)`` uniqueness uses Postgres 15+
    NULLS NOT DISTINCT so VLAN-less BRIDGE-MIB rows still de-duplicate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c4e7a2f813b9"
down_revision: str | None = "f5b9c1e8d472"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── network_device ──────────────────────────────────────────────────
    op.create_table(
        "network_device",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("ip_address", sa.dialects.postgresql.INET(), nullable=False),
        sa.Column(
            "device_type",
            sa.String(length=20),
            nullable=False,
            server_default="other",
        ),
        sa.Column("vendor", sa.String(length=64), nullable=True),
        sa.Column("sys_descr", sa.Text(), nullable=True),
        sa.Column("sys_object_id", sa.String(length=128), nullable=True),
        sa.Column("sys_name", sa.String(length=255), nullable=True),
        sa.Column("sys_uptime_seconds", sa.BigInteger(), nullable=True),
        sa.Column(
            "snmp_version",
            sa.String(length=8),
            nullable=False,
            server_default="v2c",
        ),
        sa.Column("snmp_port", sa.Integer(), nullable=False, server_default="161"),
        sa.Column(
            "snmp_timeout_seconds",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column("snmp_retries", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("community_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("v3_security_name", sa.String(length=64), nullable=True),
        sa.Column("v3_security_level", sa.String(length=20), nullable=True),
        sa.Column("v3_auth_protocol", sa.String(length=16), nullable=True),
        sa.Column("v3_auth_key_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("v3_priv_protocol", sa.String(length=16), nullable=True),
        sa.Column("v3_priv_key_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("v3_context_name", sa.String(length=64), nullable=True),
        sa.Column(
            "poll_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
        sa.Column("poll_arp", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("poll_fdb", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "poll_interfaces",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "auto_create_discovered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_poll_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("last_poll_error", sa.Text(), nullable=True),
        sa.Column("last_poll_arp_count", sa.Integer(), nullable=True),
        sa.Column("last_poll_fdb_count", sa.Integer(), nullable=True),
        sa.Column("last_poll_interface_count", sa.Integer(), nullable=True),
        sa.Column(
            "ip_space_id",
            sa.UUID(),
            sa.ForeignKey("ip_space.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.UniqueConstraint("name", name="uq_network_device_name"),
    )
    op.create_index("ix_network_device_name", "network_device", ["name"])
    op.create_index(
        "ix_network_device_next_poll_at", "network_device", ["next_poll_at"]
    )

    # ── network_interface ──────────────────────────────────────────────
    op.create_table(
        "network_interface",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "device_id",
            sa.UUID(),
            sa.ForeignKey("network_device.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("if_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("speed_bps", sa.BigInteger(), nullable=True),
        sa.Column("mac_address", sa.dialects.postgresql.MACADDR(), nullable=True),
        sa.Column("admin_status", sa.String(length=20), nullable=True),
        sa.Column("oper_status", sa.String(length=20), nullable=True),
        sa.Column("last_change_seconds", sa.Integer(), nullable=True),
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
        sa.UniqueConstraint(
            "device_id", "if_index", name="uq_network_interface_device_ifindex"
        ),
    )
    op.create_index("ix_network_interface_device", "network_interface", ["device_id"])

    # ── network_arp_entry ──────────────────────────────────────────────
    op.create_table(
        "network_arp_entry",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "device_id",
            sa.UUID(),
            sa.ForeignKey("network_device.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interface_id",
            sa.UUID(),
            sa.ForeignKey("network_interface.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ip_address", sa.dialects.postgresql.INET(), nullable=False),
        sa.Column("mac_address", sa.dialects.postgresql.MACADDR(), nullable=False),
        sa.Column("vrf_name", sa.String(length=64), nullable=True),
        sa.Column("address_type", sa.String(length=8), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
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
            "device_id", "ip_address", "vrf_name", name="uq_network_arp_device_ip_vrf"
        ),
    )
    op.create_index("ix_network_arp_device", "network_arp_entry", ["device_id"])
    op.create_index("ix_network_arp_mac", "network_arp_entry", ["mac_address"])
    op.create_index("ix_network_arp_ip", "network_arp_entry", ["ip_address"])

    # ── network_fdb_entry ──────────────────────────────────────────────
    op.create_table(
        "network_fdb_entry",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "device_id",
            sa.UUID(),
            sa.ForeignKey("network_device.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interface_id",
            sa.UUID(),
            sa.ForeignKey("network_interface.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mac_address", sa.dialects.postgresql.MACADDR(), nullable=False),
        sa.Column("vlan_id", sa.Integer(), nullable=True),
        sa.Column(
            "fdb_type",
            sa.String(length=20),
            nullable=False,
            server_default="learned",
        ),
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
    )
    op.create_index("ix_network_fdb_device", "network_fdb_entry", ["device_id"])
    op.create_index("ix_network_fdb_mac", "network_fdb_entry", ["mac_address"])
    # Postgres 15+ NULLS NOT DISTINCT — when the device only speaks
    # BRIDGE-MIB the vlan_id is NULL, but we still want the unique index
    # to treat that NULL as a value so we don't insert duplicate rows.
    op.create_index(
        "ix_network_fdb_device_mac_vlan_unique",
        "network_fdb_entry",
        ["device_id", "mac_address", "vlan_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_network_fdb_device_mac_vlan_unique", table_name="network_fdb_entry"
    )
    op.drop_index("ix_network_fdb_mac", table_name="network_fdb_entry")
    op.drop_index("ix_network_fdb_device", table_name="network_fdb_entry")
    op.drop_table("network_fdb_entry")

    op.drop_index("ix_network_arp_ip", table_name="network_arp_entry")
    op.drop_index("ix_network_arp_mac", table_name="network_arp_entry")
    op.drop_index("ix_network_arp_device", table_name="network_arp_entry")
    op.drop_table("network_arp_entry")

    op.drop_index("ix_network_interface_device", table_name="network_interface")
    op.drop_table("network_interface")

    op.drop_index("ix_network_device_next_poll_at", table_name="network_device")
    op.drop_index("ix_network_device_name", table_name="network_device")
    op.drop_table("network_device")
