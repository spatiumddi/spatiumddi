"""NetBird integration — instance table + IPAM/DNS provenance + settings toggle.

Revision ID: c9a4e1f7b820
Revises: e4b8073af215
Create Date: 2026-07-10 12:00:00

Same shape as the Tailscale + OPNsense integrations (issue #603). One
row per NetBird deployment. ``netbird_instance_id`` FK on ``ip_address``
+ ``ip_block`` + ``subnet`` (Phase 1 mirror) and ``dns_zone`` +
``dns_record`` (Phase 2 synthetic DNS), all ON DELETE CASCADE so
removing an instance sweeps its mirrored rows. Platform-settings toggle
gates the sidebar entry; the ``integrations.netbird`` feature_module row
is seeded disabled-by-default.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "c9a4e1f7b820"
down_revision: str | None = "e4b8073af215"
branch_labels = None
depends_on = None

_IPAM_TABLES = ("ip_address", "ip_block", "subnet")
_DNS_TABLES = ("dns_zone", "dns_record")


def upgrade() -> None:
    op.create_table(
        "netbird_instance",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "api_url",
            sa.String(length=255),
            nullable=False,
            server_default="https://api.netbird.io",
        ),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
            "network_cidr",
            sa.String(length=32),
            nullable=False,
            server_default="100.64.0.0/10",
        ),
        sa.Column("skip_expired", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("dns_domain", sa.String(length=255), nullable=True),
        sa.Column("peer_count", sa.Integer(), nullable=True),
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
    op.create_index("ix_netbird_instance_name", "netbird_instance", ["name"], unique=True)

    for table in _IPAM_TABLES + _DNS_TABLES:
        op.add_column(
            table,
            sa.Column(
                "netbird_instance_id",
                sa.UUID(),
                sa.ForeignKey("netbird_instance.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_netbird_instance_id", table, ["netbird_instance_id"])

    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_netbird_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Seed the feature_module row disabled-by-default. Idempotent — the
    # catalog's default_enabled=False already resolves to "off" without a
    # row, but seeding keeps a fresh migrate consistent with the model.
    op.execute(
        sa.text(
            "INSERT INTO feature_module (id, enabled) "
            "VALUES ('integrations.netbird', false) ON CONFLICT (id) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'integrations.netbird'"))
    op.drop_column("platform_settings", "integration_netbird_enabled")
    for table in tuple(reversed(_IPAM_TABLES + _DNS_TABLES)):
        op.drop_index(f"ix_{table}_netbird_instance_id", table_name=table)
        op.drop_column(table, "netbird_instance_id")
    op.drop_index("ix_netbird_instance_name", table_name="netbird_instance")
    op.drop_table("netbird_instance")
