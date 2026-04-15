"""add DHCP models: server groups, servers, scopes, pools, statics, client classes,
leases, and config-op queue.

Revision ID: d9a4c3b7e812
Revises:
Create Date: 2026-04-15 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d9a4c3b7e812"
down_revision: str | None = "a9b3c7d5e2f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── dhcp_server_group ───────────────────────────────────────────────────
    op.create_table(
        "dhcp_server_group",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "mode",
            sa.String(20),
            nullable=False,
            server_default="hot-standby",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_dhcp_server_group_name", "dhcp_server_group", ["name"])

    # ── dhcp_server ─────────────────────────────────────────────────────────
    op.create_table(
        "dhcp_server",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("driver", sa.String(50), nullable=False, server_default="kea"),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="67"),
        sa.Column("roles", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "server_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server_group.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "agent_registered", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("agent_token_hash", sa.String(128), nullable=True),
        sa.Column("agent_last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_version", sa.String(64), nullable=True),
        sa.Column(
            "agent_approved", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("agent_fingerprint", sa.String(128), nullable=True),
        sa.Column("config_etag", sa.String(128), nullable=True),
        sa.Column("config_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_dhcp_server_name"),
    )
    op.create_index("ix_dhcp_server_name", "dhcp_server", ["name"])
    op.create_index("ix_dhcp_server_group", "dhcp_server", ["server_group_id"])
    op.create_index("ix_dhcp_server_agent_id", "dhcp_server", ["agent_id"], unique=True)

    # ── dhcp_scope ──────────────────────────────────────────────────────────
    op.create_table(
        "dhcp_scope",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subnet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subnet.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("lease_time", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("min_lease_time", sa.Integer(), nullable=True),
        sa.Column("max_lease_time", sa.Integer(), nullable=True),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "ddns_enabled", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "ddns_hostname_policy",
            sa.String(30),
            nullable=False,
            server_default="client",
        ),
        sa.Column(
            "hostname_to_ipam_sync",
            sa.String(30),
            nullable=False,
            server_default="on_static_only",
        ),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "server_id", "subnet_id", name="uq_dhcp_scope_server_subnet"
        ),
    )
    op.create_index("ix_dhcp_scope_server", "dhcp_scope", ["server_id"])
    op.create_index("ix_dhcp_scope_subnet", "dhcp_scope", ["subnet_id"])

    # ── dhcp_pool ───────────────────────────────────────────────────────────
    op.create_table(
        "dhcp_pool",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scope_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_scope.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("start_ip", postgresql.INET(), nullable=False),
        sa.Column("end_ip", postgresql.INET(), nullable=False),
        sa.Column(
            "pool_type", sa.String(20), nullable=False, server_default="dynamic"
        ),
        sa.Column("class_restriction", sa.String(255), nullable=True),
        sa.Column("lease_time_override", sa.Integer(), nullable=True),
        sa.Column("options_override", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_dhcp_pool_scope", "dhcp_pool", ["scope_id"])

    # ── dhcp_static_assignment ──────────────────────────────────────────────
    op.create_table(
        "dhcp_static_assignment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scope_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_scope.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=False),
        sa.Column("mac_address", postgresql.MACADDR(), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=True),
        sa.Column("hostname", sa.String(255), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("options_override", postgresql.JSONB(), nullable=True),
        sa.Column(
            "ip_address_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ip_address.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("scope_id", "mac_address", name="uq_dhcp_static_scope_mac"),
        sa.UniqueConstraint("scope_id", "ip_address", name="uq_dhcp_static_scope_ip"),
    )
    op.create_index("ix_dhcp_static_scope", "dhcp_static_assignment", ["scope_id"])
    op.create_index("ix_dhcp_static_mac", "dhcp_static_assignment", ["mac_address"])
    op.create_index(
        "ix_dhcp_static_ip_address_id", "dhcp_static_assignment", ["ip_address_id"]
    )

    # ── dhcp_client_class ───────────────────────────────────────────────────
    op.create_table(
        "dhcp_client_class",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("match_expression", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "server_id", "name", name="uq_dhcp_client_class_server_name"
        ),
    )
    op.create_index("ix_dhcp_client_class_server", "dhcp_client_class", ["server_id"])

    # ── dhcp_lease ──────────────────────────────────────────────────────────
    op.create_table(
        "dhcp_lease",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scope_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_scope.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=False),
        sa.Column("mac_address", postgresql.MACADDR(), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=True),
        sa.Column("client_id", sa.String(255), nullable=True),
        sa.Column("user_class", sa.String(255), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_dhcp_lease_server_ip", "dhcp_lease", ["server_id", "ip_address"])
    op.create_index("ix_dhcp_lease_server_mac", "dhcp_lease", ["server_id", "mac_address"])
    op.create_index("ix_dhcp_lease_state", "dhcp_lease", ["state"])

    # ── dhcp_config_op ──────────────────────────────────────────────────────
    op.create_table(
        "dhcp_config_op",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("op_type", sa.String(30), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_dhcp_config_op_server_status",
        "dhcp_config_op",
        ["server_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_dhcp_config_op_server_status", table_name="dhcp_config_op")
    op.drop_table("dhcp_config_op")
    op.drop_index("ix_dhcp_lease_state", table_name="dhcp_lease")
    op.drop_index("ix_dhcp_lease_server_mac", table_name="dhcp_lease")
    op.drop_index("ix_dhcp_lease_server_ip", table_name="dhcp_lease")
    op.drop_table("dhcp_lease")
    op.drop_index("ix_dhcp_client_class_server", table_name="dhcp_client_class")
    op.drop_table("dhcp_client_class")
    op.drop_index("ix_dhcp_static_ip_address_id", table_name="dhcp_static_assignment")
    op.drop_index("ix_dhcp_static_mac", table_name="dhcp_static_assignment")
    op.drop_index("ix_dhcp_static_scope", table_name="dhcp_static_assignment")
    op.drop_table("dhcp_static_assignment")
    op.drop_index("ix_dhcp_pool_scope", table_name="dhcp_pool")
    op.drop_table("dhcp_pool")
    op.drop_index("ix_dhcp_scope_subnet", table_name="dhcp_scope")
    op.drop_index("ix_dhcp_scope_server", table_name="dhcp_scope")
    op.drop_table("dhcp_scope")
    op.drop_index("ix_dhcp_server_agent_id", table_name="dhcp_server")
    op.drop_index("ix_dhcp_server_group", table_name="dhcp_server")
    op.drop_index("ix_dhcp_server_name", table_name="dhcp_server")
    op.drop_table("dhcp_server")
    op.drop_index("ix_dhcp_server_group_name", table_name="dhcp_server_group")
    op.drop_table("dhcp_server_group")
