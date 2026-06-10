"""OPNsense integration — firewall table + IPAM provenance + settings toggle.

Revision ID: b6f4d2a91c83
Revises: a3e7c9d12f80
Create Date: 2026-06-09 12:00:00

Same shape as the Proxmox + Tailscale integrations. One row per
OPNsense firewall. ``opnsense_router_id`` FK on ``ip_address`` +
``ip_block`` + ``subnet`` with ON DELETE CASCADE so removing a
firewall sweeps its mirrored IPAM rows. Platform-settings toggle gates
the sidebar entry; the ``integrations.opnsense`` feature_module row is
seeded disabled-by-default.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "b6f4d2a91c83"
down_revision: str | None = "a3e7c9d12f80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opnsense_router",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="443"),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("ca_bundle_pem", sa.Text(), nullable=False, server_default=""),
        sa.Column("api_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "api_secret_encrypted",
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
            "mirror_dhcp_leases", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "mirror_static_mappings", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("mirror_arp", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("firmware_version", sa.String(length=64), nullable=True),
        sa.Column("interface_count", sa.Integer(), nullable=True),
        sa.Column("lease_count", sa.Integer(), nullable=True),
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
    op.create_index("ix_opnsense_router_name", "opnsense_router", ["name"], unique=True)

    for table in ("ip_address", "ip_block", "subnet"):
        op.add_column(
            table,
            sa.Column(
                "opnsense_router_id",
                sa.UUID(),
                sa.ForeignKey("opnsense_router.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_opnsense_router_id", table, ["opnsense_router_id"])

    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_opnsense_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Seed the feature_module row disabled-by-default. Idempotent — the
    # startup sync also reconciles MODULES, but seeding here keeps a
    # fresh migrate consistent with the model.
    op.execute(
        sa.text(
            "INSERT INTO feature_module (id, enabled) "
            "VALUES ('integrations.opnsense', false) ON CONFLICT (id) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'integrations.opnsense'"))
    op.drop_column("platform_settings", "integration_opnsense_enabled")
    for table in ("subnet", "ip_block", "ip_address"):
        op.drop_index(f"ix_{table}_opnsense_router_id", table_name=table)
        op.drop_column(table, "opnsense_router_id")
    op.drop_index("ix_opnsense_router_name", table_name="opnsense_router")
    op.drop_table("opnsense_router")
