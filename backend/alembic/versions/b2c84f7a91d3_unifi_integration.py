"""UniFi Network integration — controller table + IPAM provenance + settings toggle + feature_module seed.

Revision ID: b2c84f7a91d3
Revises: d8b5e4a91f27
Create Date: 2026-05-06 14:00:00

Same shape as the Kubernetes / Docker / Proxmox / Tailscale
integrations. One ``unifi_controller`` row per controller (local or
cloud); the controller may own many sites. ``unifi_controller_id``
FK on ``ip_address`` + ``ip_block`` + ``subnet`` with ON DELETE
CASCADE so removing a controller sweeps its mirrored IPAM rows.

Adds ``integration_unifi_enabled`` to ``platform_settings`` (kept
in lock-step with the ``integrations.unifi`` feature_module by the
toggle endpoint) and seeds the matching ``feature_module`` row at
``enabled=False`` so the integration is dormant until an operator
opts in.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b2c84f7a91d3"
down_revision: str | None = "d8b5e4a91f27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "unifi_controller",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # Transport
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="local"),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("port", sa.Integer(), nullable=False, server_default="443"),
        sa.Column("cloud_host_id", sa.String(length=64), nullable=True),
        sa.Column("verify_tls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("ca_bundle_pem", sa.Text(), nullable=False, server_default=""),
        # Auth
        sa.Column("auth_kind", sa.String(length=32), nullable=False, server_default="api_key"),
        sa.Column(
            "api_key_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
        sa.Column(
            "username_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
        sa.Column(
            "password_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
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
            "mirror_networks", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "mirror_clients", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "mirror_fixed_ips", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "site_allowlist",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "network_allowlist",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("include_wired", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "include_wireless", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("include_vpn", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # Cadence
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        # Sync state
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("controller_version", sa.String(length=64), nullable=True),
        sa.Column("site_count", sa.Integer(), nullable=True),
        sa.Column("network_count", sa.Integer(), nullable=True),
        sa.Column("client_count", sa.Integer(), nullable=True),
        sa.Column("last_discovery", sa.dialects.postgresql.JSONB(), nullable=True),
        # Timestamps
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
    op.create_index("ix_unifi_controller_name", "unifi_controller", ["name"], unique=True)

    for table in ("ip_address", "ip_block", "subnet"):
        op.add_column(
            table,
            sa.Column(
                "unifi_controller_id",
                sa.UUID(),
                sa.ForeignKey("unifi_controller.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_unifi_controller_id", table, ["unifi_controller_id"])

    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_unifi_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Seed the feature_module row at enabled=False — operators opt
    # in via Settings → Features. Idempotent.
    op.execute(
        sa.text(
            "INSERT INTO feature_module (id, enabled) "
            "VALUES (:id, :enabled) ON CONFLICT (id) DO NOTHING"
        ).bindparams(id="integrations.unifi", enabled=False)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM feature_module WHERE id = :id").bindparams(id="integrations.unifi")
    )
    op.drop_column("platform_settings", "integration_unifi_enabled")
    for table in ("subnet", "ip_block", "ip_address"):
        op.drop_index(f"ix_{table}_unifi_controller_id", table_name=table)
        op.drop_column(table, "unifi_controller_id")
    op.drop_index("ix_unifi_controller_name", table_name="unifi_controller")
    op.drop_table("unifi_controller")
