"""Active block sync (#601) — network_block desired-state + per-target
push state + OPNsense/UniFi enforcement columns + feature module seed.

Revision ID: d3b9f42a1c05
Revises: c9a4e1f7b820
Create Date: 2026-07-10 13:00:00

The enforcement half of the detect→block loop. ``network_block`` is the
SpatiumDDI-owned block set (IPs / MACs); ``network_block_push`` tracks
per-(block, target) convergence. Enforcement columns are added to the
existing read-only mirror rows (``opnsense_router`` / ``unifi_controller``)
so a target is armed in-place — the mirror itself stays read-only. The
``security.block_sync`` feature module is seeded disabled-by-default.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "d3b9f42a1c05"
down_revision: str | None = "c9a4e1f7b820"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "network_block",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False, server_default="quarantine"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.UUID(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_user_id",
            sa.UUID(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
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
        sa.UniqueConstraint("kind", "value", name="uq_network_block_kind_value"),
    )
    op.create_index("ix_network_block_enabled", "network_block", ["enabled"])

    op.create_table(
        "network_block_push",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "block_id",
            sa.UUID(),
            sa.ForeignKey("network_block.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column(
            "push_status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.UniqueConstraint(
            "block_id", "target_kind", "target_id", name="uq_network_block_push_block_target"
        ),
    )
    op.create_index(
        "ix_network_block_push_target", "network_block_push", ["target_kind", "target_id"]
    )

    # OPNsense enforcement columns (firewall alias membership).
    op.add_column(
        "opnsense_router",
        sa.Column(
            "block_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "opnsense_router",
        sa.Column("block_sync_api_key", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "opnsense_router",
        sa.Column(
            "block_sync_api_secret_encrypted",
            sa.LargeBinary(),
            nullable=False,
            server_default=sa.text("''::bytea"),
        ),
    )
    op.add_column(
        "opnsense_router",
        sa.Column("block_alias_name", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "opnsense_router",
        sa.Column("last_block_sync_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("opnsense_router", sa.Column("last_block_sync_error", sa.Text(), nullable=True))

    # UniFi enforcement columns (L2 client quarantine).
    op.add_column(
        "unifi_controller",
        sa.Column(
            "block_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "unifi_controller",
        sa.Column(
            "block_sync_auth_kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'api_key'"),
        ),
    )
    for col in (
        "block_sync_api_key_encrypted",
        "block_sync_username_encrypted",
        "block_sync_password_encrypted",
    ):
        op.add_column(
            "unifi_controller",
            sa.Column(col, sa.LargeBinary(), nullable=False, server_default=sa.text("''::bytea")),
        )
    op.add_column(
        "unifi_controller",
        sa.Column(
            "block_sync_site", sa.String(length=64), nullable=False, server_default="default"
        ),
    )
    op.add_column(
        "unifi_controller",
        sa.Column("last_block_sync_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("unifi_controller", sa.Column("last_block_sync_error", sa.Text(), nullable=True))

    # Seed the feature module disabled-by-default (idempotent).
    op.execute(
        sa.text(
            "INSERT INTO feature_module (id, enabled) "
            "VALUES ('security.block_sync', false) ON CONFLICT (id) DO NOTHING"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM feature_module WHERE id = 'security.block_sync'"))
    for col in (
        "last_block_sync_error",
        "last_block_sync_at",
        "block_sync_site",
        "block_sync_password_encrypted",
        "block_sync_username_encrypted",
        "block_sync_api_key_encrypted",
        "block_sync_auth_kind",
        "block_sync_enabled",
    ):
        op.drop_column("unifi_controller", col)
    for col in (
        "last_block_sync_error",
        "last_block_sync_at",
        "block_alias_name",
        "block_sync_api_secret_encrypted",
        "block_sync_api_key",
        "block_sync_enabled",
    ):
        op.drop_column("opnsense_router", col)
    op.drop_index("ix_network_block_push_target", table_name="network_block_push")
    op.drop_table("network_block_push")
    op.drop_index("ix_network_block_enabled", table_name="network_block")
    op.drop_table("network_block")
