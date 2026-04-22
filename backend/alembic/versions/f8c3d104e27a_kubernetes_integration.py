"""Kubernetes integration — Phase 1a (cluster connection table).

Revision ID: f8c3d104e27a
Revises: e5b21a8f0d94
Create Date: 2026-04-22 19:45:00

Adds:
  * ``platform_settings.integration_kubernetes_enabled`` — master toggle
    that gates the sidebar nav item + cluster admin page. Off by default;
    only deployments running Kubernetes integration turn it on.
  * ``kubernetes_cluster`` — per-cluster config (connection URL + CA
    bundle + encrypted bearer token) plus the IPAM space / DNS group
    binding. Sync state columns are included but stay null until
    Phase 1b lands the reconciler.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f8c3d104e27a"
down_revision: str | None = "e5b21a8f0d94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "integration_kubernetes_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        "kubernetes_cluster",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("api_server_url", sa.String(length=500), nullable=False),
        sa.Column("ca_bundle_pem", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_encrypted", sa.LargeBinary(), nullable=False),
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
        sa.Column("pod_cidr", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("service_cidr", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "sync_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default="60",
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("cluster_version", sa.String(length=64), nullable=True),
        sa.Column("node_count", sa.Integer(), nullable=True),
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
    )
    op.create_index(
        "ix_kubernetes_cluster_name",
        "kubernetes_cluster",
        ["name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_kubernetes_cluster_name", table_name="kubernetes_cluster")
    op.drop_table("kubernetes_cluster")
    op.drop_column("platform_settings", "integration_kubernetes_enabled")
