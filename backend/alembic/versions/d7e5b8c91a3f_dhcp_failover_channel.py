"""DHCP failover channel (Kea HA) + ha_state columns on DHCPServer.

Revision ID: d7e5b8c91a3f
Revises: c3a81f9d6b42
Create Date: 2026-04-21 19:00:00

Adds:
  * ``dhcp_failover_channel`` — one row per Kea HA relationship between
    two DHCP servers. Carries the mode (``hot-standby`` /
    ``load-balancing``), heartbeat tuning knobs, and each peer's
    control-agent URL. Unique FK constraints on primary_server_id and
    secondary_server_id enforce the "a server is in at most one HA
    relationship" rule Kea itself requires.
  * ``dhcp_server.ha_state`` + ``dhcp_server.ha_last_heartbeat_at`` —
    reported by the agent's periodic ``ha-status-get`` poll. Null on
    servers not in a channel.

The ``server_group`` table's pre-existing ``mode`` column is left
alone; it's unused today (no driver branch reads it) and we keep the
new channel as the single source of truth for HA configuration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d7e5b8c91a3f"
down_revision: str | None = "c3a81f9d6b42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dhcp_failover_channel",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "description", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "mode",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'hot-standby'"),
        ),
        sa.Column(
            "primary_server_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "secondary_server_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "primary_peer_url",
            sa.String(length=512),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "secondary_peer_url",
            sa.String(length=512),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "heartbeat_delay_ms",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10000"),
        ),
        sa.Column(
            "max_response_delay_ms",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60000"),
        ),
        sa.Column(
            "max_ack_delay_ms",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10000"),
        ),
        sa.Column(
            "max_unacked_clients",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("5"),
        ),
        sa.Column(
            "auto_failover",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
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
        sa.UniqueConstraint("name", name="uq_dhcp_failover_channel_name"),
        sa.UniqueConstraint("primary_server_id", name="uq_dhcp_failover_primary"),
        sa.UniqueConstraint("secondary_server_id", name="uq_dhcp_failover_secondary"),
    )
    op.create_index(
        "ix_dhcp_failover_channel_name",
        "dhcp_failover_channel",
        ["name"],
        unique=False,
    )

    op.add_column(
        "dhcp_server",
        sa.Column("ha_state", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "dhcp_server",
        sa.Column(
            "ha_last_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dhcp_server", "ha_last_heartbeat_at")
    op.drop_column("dhcp_server", "ha_state")
    op.drop_index("ix_dhcp_failover_channel_name", table_name="dhcp_failover_channel")
    op.drop_table("dhcp_failover_channel")
