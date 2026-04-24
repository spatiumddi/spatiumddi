"""Proxmox VE integration — endpoint table + IPAM provenance + settings toggle.

Revision ID: d1a8f3c704e9
Revises: c9e2b0d3a5f7
Create Date: 2026-04-23 12:00:00

Same shape as the Docker + Kubernetes integrations. One row per PVE
endpoint (a single host or any node of a cluster — the REST API is
homogeneous across cluster members). ``proxmox_node_id`` FK on
``ip_address`` + ``ip_block`` + ``subnet`` with ON DELETE CASCADE so
removing an endpoint sweeps its mirrored IPAM rows. Platform-
settings toggle gates the sidebar entry.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d1a8f3c704e9"
down_revision: str | None = "c9e2b0d3a5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proxmox_node",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="8006"),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("ca_bundle_pem", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "token_secret_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
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
        sa.Column("mirror_vms", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("mirror_lxc", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "include_stopped", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("pve_version", sa.String(length=64), nullable=True),
        sa.Column("cluster_name", sa.String(length=255), nullable=True),
        sa.Column("node_count", sa.Integer(), nullable=True),
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
    op.create_index("ix_proxmox_node_name", "proxmox_node", ["name"], unique=True)

    for table in ("ip_address", "ip_block", "subnet"):
        op.add_column(
            table,
            sa.Column(
                "proxmox_node_id",
                sa.UUID(),
                sa.ForeignKey("proxmox_node.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_proxmox_node_id", table, ["proxmox_node_id"])

    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_proxmox_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "integration_proxmox_enabled")
    for table in ("subnet", "ip_block", "ip_address"):
        op.drop_index(f"ix_{table}_proxmox_node_id", table_name=table)
        op.drop_column(table, "proxmox_node_id")
    op.drop_index("ix_proxmox_node_name", table_name="proxmox_node")
    op.drop_table("proxmox_node")
