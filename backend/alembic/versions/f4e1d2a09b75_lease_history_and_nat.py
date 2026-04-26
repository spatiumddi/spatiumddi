"""DHCP lease history + NAT mapping.

Revision ID: f4e1d2a09b75
Revises: e6f12b9a3c84
Create Date: 2026-04-26 12:00:00

Two unrelated additions bundled in one migration so Wave-N is one
schema bump:

  * ``dhcp_lease_history`` — append-only row written when an active
    lease leaves the active set (``expired`` / ``released`` / ``removed``
    / ``superseded``). Pruned by the daily
    ``dhcp_lease_history_prune.prune_lease_history`` Celery task per
    ``platform_settings.dhcp_lease_history_retention_days`` (default
    90 d, 0 = keep forever).

  * ``nat_mapping`` — operator-curated NAT mapping records (``1to1``,
    ``pat``, ``hide``). Cross-referenced from ``IPAddress`` rows via
    a count-only join in the IPAM list endpoint; the actual rules
    aren't pushed anywhere — this is visibility metadata only.

Both tables hang off existing rows with cascade semantics that match
their parents (lease history follows the server, NAT mapping
``internal_subnet_id`` SET-NULLs on subnet delete because the mapping
itself outlives the IPAM model).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4e1d2a09b75"
down_revision: str | None = "c1f4a8b27d09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── dhcp_lease_history ──
    op.create_table(
        "dhcp_lease_history",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "server_id",
            sa.UUID(),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scope_id",
            sa.UUID(),
            sa.ForeignKey("dhcp_scope.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=False),
        sa.Column("mac_address", postgresql.MACADDR(), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("client_id", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "expired_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_state", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_dhcp_lease_history_server_id", "dhcp_lease_history", ["server_id"])
    op.create_index("ix_dhcp_lease_history_scope_id", "dhcp_lease_history", ["scope_id"])
    op.create_index("ix_dhcp_lease_history_ip_address", "dhcp_lease_history", ["ip_address"])
    op.create_index("ix_dhcp_lease_history_mac_address", "dhcp_lease_history", ["mac_address"])
    op.create_index(
        "ix_dhcp_lease_history_server_expired",
        "dhcp_lease_history",
        ["server_id", "expired_at"],
    )

    # ── platform_settings.dhcp_lease_history_retention_days ──
    op.add_column(
        "platform_settings",
        sa.Column(
            "dhcp_lease_history_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="90",
        ),
    )

    # ── nat_mapping ──
    op.create_table(
        "nat_mapping",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("internal_ip", postgresql.INET(), nullable=True),
        sa.Column(
            "internal_subnet_id",
            sa.UUID(),
            sa.ForeignKey("subnet.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("internal_port_start", sa.Integer(), nullable=True),
        sa.Column("internal_port_end", sa.Integer(), nullable=True),
        sa.Column("external_ip", postgresql.INET(), nullable=True),
        sa.Column("external_port_start", sa.Integer(), nullable=True),
        sa.Column("external_port_end", sa.Integer(), nullable=True),
        sa.Column(
            "protocol",
            sa.String(length=10),
            nullable=False,
            server_default="any",
        ),
        sa.Column("device_label", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
    )
    op.create_index("ix_nat_mapping_internal_ip", "nat_mapping", ["internal_ip"])
    op.create_index("ix_nat_mapping_external_ip", "nat_mapping", ["external_ip"])
    op.create_index("ix_nat_mapping_internal_subnet_id", "nat_mapping", ["internal_subnet_id"])
    op.create_index("ix_nat_mapping_kind", "nat_mapping", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_nat_mapping_kind", table_name="nat_mapping")
    op.drop_index("ix_nat_mapping_internal_subnet_id", table_name="nat_mapping")
    op.drop_index("ix_nat_mapping_external_ip", table_name="nat_mapping")
    op.drop_index("ix_nat_mapping_internal_ip", table_name="nat_mapping")
    op.drop_table("nat_mapping")

    op.drop_column("platform_settings", "dhcp_lease_history_retention_days")

    op.drop_index("ix_dhcp_lease_history_server_expired", table_name="dhcp_lease_history")
    op.drop_index("ix_dhcp_lease_history_mac_address", table_name="dhcp_lease_history")
    op.drop_index("ix_dhcp_lease_history_ip_address", table_name="dhcp_lease_history")
    op.drop_index("ix_dhcp_lease_history_scope_id", table_name="dhcp_lease_history")
    op.drop_index("ix_dhcp_lease_history_server_id", table_name="dhcp_lease_history")
    op.drop_table("dhcp_lease_history")
