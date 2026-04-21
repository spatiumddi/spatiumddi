"""Per-server DNS zone serial reporting.

Revision ID: b4d291a6c7e8
Revises: a8d31e6b2f47
Create Date: 2026-04-21 19:00:00

Adds the ``dns_server_zone_state`` table — one row per
``(server_id, zone_id)`` pair. Agents upsert their most-recently
rendered serial here after a successful config apply; the UI renders
a "3/3 servers on serial 42" pill (or a drift warning) by comparing
these rows to ``dns_zone.serial`` and to each other within a group.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b4d291a6c7e8"
down_revision: str | None = "a8d31e6b2f47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dns_server_zone_state",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "server_id",
            sa.UUID(),
            sa.ForeignKey("dns_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "zone_id",
            sa.UUID(),
            sa.ForeignKey("dns_zone.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("current_serial", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "reported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_dns_server_zone_state_server_id",
        "dns_server_zone_state",
        ["server_id"],
    )
    op.create_index(
        "ix_dns_server_zone_state_zone",
        "dns_server_zone_state",
        ["zone_id"],
    )
    op.create_unique_constraint(
        "uq_dns_server_zone_state",
        "dns_server_zone_state",
        ["server_id", "zone_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_dns_server_zone_state", "dns_server_zone_state", type_="unique"
    )
    op.drop_index(
        "ix_dns_server_zone_state_zone", table_name="dns_server_zone_state"
    )
    op.drop_index(
        "ix_dns_server_zone_state_server_id", table_name="dns_server_zone_state"
    )
    op.drop_table("dns_server_zone_state")
