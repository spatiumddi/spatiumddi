"""Tailscale integration — tenant table + IPAM provenance + settings toggle.

Revision ID: c4e1a87b3920
Revises: f8d4e29b1c75
Create Date: 2026-04-25 12:00:00

Same shape as the Proxmox / Docker / Kubernetes integrations. One
row per tailnet (PAT + tailnet slug). ``tailscale_tenant_id`` FK on
``ip_address`` + ``ip_block`` + ``subnet`` with ON DELETE CASCADE so
removing a tenant sweeps its mirrored IPAM rows. Platform-settings
toggle gates the sidebar entry.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c4e1a87b3920"
down_revision: str | None = "f8d4e29b1c75"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tailscale_tenant",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("tailnet", sa.String(length=255), nullable=False, server_default="-"),
        sa.Column(
            "api_key_encrypted",
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
        sa.Column(
            "cgnat_cidr",
            sa.String(length=32),
            nullable=False,
            server_default="100.64.0.0/10",
        ),
        sa.Column(
            "ipv6_cidr",
            sa.String(length=64),
            nullable=False,
            server_default="fd7a:115c:a1e0::/48",
        ),
        sa.Column(
            "skip_expired", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("tailnet_domain", sa.String(length=255), nullable=True),
        sa.Column("device_count", sa.Integer(), nullable=True),
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
    op.create_index(
        "ix_tailscale_tenant_name", "tailscale_tenant", ["name"], unique=True
    )

    for table in ("ip_address", "ip_block", "subnet"):
        op.add_column(
            table,
            sa.Column(
                "tailscale_tenant_id",
                sa.UUID(),
                sa.ForeignKey("tailscale_tenant.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_tailscale_tenant_id", table, ["tailscale_tenant_id"]
        )

    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_tailscale_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "integration_tailscale_enabled")
    for table in ("subnet", "ip_block", "ip_address"):
        op.drop_index(f"ix_{table}_tailscale_tenant_id", table_name=table)
        op.drop_column(table, "tailscale_tenant_id")
    op.drop_index("ix_tailscale_tenant_name", table_name="tailscale_tenant")
    op.drop_table("tailscale_tenant")
