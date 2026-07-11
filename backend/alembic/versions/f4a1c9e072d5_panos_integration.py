"""Palo Alto PAN-OS / Panorama integration (#605).

Creates ``panos_firewall`` (one row per managed scope — standalone vsys or
Panorama device-group) carrying both the read-only mirror config and the
opt-in DAG-enforcement (#601 tier) columns, and ``firewall_endpoint_object``
(the mirrored address-object "shadow IPAM" store). Adds ``panos_firewall_id``
provenance FK on ``ip_address`` / ``ip_block`` / ``subnet`` / ``nat_mapping``
(all ON DELETE CASCADE so removing a firewall sweeps its mirrored rows).
Adds the ``platform_settings.integration_panos_enabled`` toggle and seeds the
``integrations.paloalto`` feature_module row disabled-by-default.

Revision ID: f4a1c9e072d5
Revises: d3b9f42a1c05
Create Date: 2026-07-10 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f4a1c9e072d5"
down_revision: str | None = "d3b9f42a1c05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "panos_firewall",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # Connection
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="443"),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("ca_bundle_pem", sa.Text(), nullable=False, server_default=""),
        sa.Column("api_version", sa.String(length=8), nullable=False, server_default="10.1"),
        sa.Column(
            "api_key_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
        # Scoping
        sa.Column("is_panorama", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("vsys", sa.String(length=64), nullable=False, server_default="vsys1"),
        sa.Column("device_group", sa.String(length=255), nullable=False, server_default=""),
        # Binding
        sa.Column(
            "ipam_space_id",
            sa.UUID(),
            sa.ForeignKey("ip_space.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "dns_group_id",
            sa.UUID(),
            sa.ForeignKey("dns_server_group.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Mirror policy
        sa.Column(
            "mirror_address_objects", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("mirror_nat_rules", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "mirror_interfaces", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "mirror_dhcp_leases", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        # DAG enforcement (#601 tier)
        sa.Column(
            "block_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "block_sync_api_key_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
        sa.Column(
            "block_tag_name",
            sa.String(length=127),
            nullable=False,
            server_default="spatiumddi-quarantine",
        ),
        sa.Column("last_block_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_block_sync_error", sa.Text(), nullable=True),
        # Sync state
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("sw_version", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("object_count", sa.Integer(), nullable=True),
        sa.Column("nat_rule_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_panos_firewall_name", "panos_firewall", ["name"], unique=True)

    op.create_table(
        "firewall_endpoint_object",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "panos_firewall_id",
            sa.UUID(),
            sa.ForeignKey("panos_firewall.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("resolved_cidr", postgresql.INET(), nullable=True),
        sa.Column(
            "ip_address_id",
            sa.UUID(),
            sa.ForeignKey("ip_address.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "subnet_id",
            sa.UUID(),
            sa.ForeignKey("subnet.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("panos_firewall_id", "name", name="uq_firewall_object_fw_name"),
    )
    op.create_index(
        "ix_firewall_object_panos_firewall_id",
        "firewall_endpoint_object",
        ["panos_firewall_id"],
    )
    op.create_index("ix_firewall_object_value", "firewall_endpoint_object", ["value"])

    for table in ("ip_address", "ip_block", "subnet", "nat_mapping"):
        op.add_column(
            table,
            sa.Column(
                "panos_firewall_id",
                sa.UUID(),
                sa.ForeignKey("panos_firewall.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_panos_firewall_id", table, ["panos_firewall_id"])

    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_panos_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.execute(
        sa.text(
            "INSERT INTO feature_module (id, enabled) "
            "VALUES ('integrations.paloalto', false) ON CONFLICT (id) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'integrations.paloalto'"))
    op.drop_column("platform_settings", "integration_panos_enabled")
    for table in ("nat_mapping", "subnet", "ip_block", "ip_address"):
        op.drop_index(f"ix_{table}_panos_firewall_id", table_name=table)
        op.drop_column(table, "panos_firewall_id")
    op.drop_index("ix_firewall_object_value", table_name="firewall_endpoint_object")
    op.drop_index("ix_firewall_object_panos_firewall_id", table_name="firewall_endpoint_object")
    op.drop_table("firewall_endpoint_object")
    op.drop_index("ix_panos_firewall_name", table_name="panos_firewall")
    op.drop_table("panos_firewall")
