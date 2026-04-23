"""Docker integration — hosts table + IPAM provenance + settings toggle.

Revision ID: c9e2b0d3a5f7
Revises: b5c2704e11af
Create Date: 2026-04-22 21:45:00

Same shape as the Kubernetes integration (``f8c3d104e27a`` +
``a917b4c9e251`` + ``b5c2704e11af``) — one host row per connected
daemon, ``docker_host_id`` FK on ``ip_address`` + ``ip_block`` +
``subnet`` with ON DELETE CASCADE so removing a host sweeps its
mirrored IPAM rows, and a platform-settings toggle so the
integration's sidebar row appears only when enabled.

Docker bridge networks carry normal LAN semantics (gateway,
broadcast). No ``docker_semantics`` flag — reconciler-created
subnets get the usual network/broadcast/gateway placeholder rows
just like operator-created ones.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c9e2b0d3a5f7"
down_revision: str | None = "b5c2704e11af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "docker_host",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "connection_type",
            sa.String(length=16),
            nullable=False,
            server_default="tcp",
        ),
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("ca_bundle_pem", sa.Text(), nullable=False, server_default=""),
        sa.Column("client_cert_pem", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "client_key_encrypted",
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
            "mirror_containers",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "include_default_networks",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "include_stopped_containers",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("sync_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("engine_version", sa.String(length=64), nullable=True),
        sa.Column("container_count", sa.Integer(), nullable=True),
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
        "ix_docker_host_name", "docker_host", ["name"], unique=True
    )

    # Provenance FKs on IPAM rows — same cascade semantics as the
    # Kubernetes FKs set up in a917b4c9e251 / b5c2704e11af.
    for table in ("ip_address", "ip_block", "subnet"):
        op.add_column(
            table,
            sa.Column(
                "docker_host_id",
                sa.UUID(),
                sa.ForeignKey("docker_host.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            f"ix_{table}_docker_host_id",
            table,
            ["docker_host_id"],
        )

    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_docker_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "integration_docker_enabled")
    for table in ("subnet", "ip_block", "ip_address"):
        op.drop_index(f"ix_{table}_docker_host_id", table_name=table)
        op.drop_column(table, "docker_host_id")
    op.drop_index("ix_docker_host_name", table_name="docker_host")
    op.drop_table("docker_host")
