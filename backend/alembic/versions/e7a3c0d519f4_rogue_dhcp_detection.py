"""Rogue DHCP server detection (#370)

Adds ``dhcp_observed_responder`` (DHCP servers the agent's active probe saw
answering on a segment, with an expected/acknowledged/rogue classification)
and ``dhcp_responder_allowlist`` (operator-acknowledged responders).

Revision ID: e7a3c0d519f4
Revises: d5b2e8a14c93
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e7a3c0d519f4"
down_revision = "d5b2e8a14c93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dhcp_observed_responder",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reported_by_server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("server_identifier", sa.String(length=64), nullable=False),
        sa.Column("source_ip", postgresql.INET(), nullable=False),
        sa.Column("source_mac", postgresql.MACADDR(), nullable=True),
        sa.Column("giaddr", postgresql.INET(), nullable=True),
        sa.Column("offered_ip", postgresql.INET(), nullable=True),
        sa.Column(
            "classification",
            sa.String(length=16),
            nullable=False,
            server_default="rogue",
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "group_id", "server_identifier", "source_ip", name="uq_dhcp_responder_id_ip"
        ),
    )
    op.create_index("ix_dhcp_observed_responder_group", "dhcp_observed_responder", ["group_id"])
    op.create_index(
        "ix_dhcp_observed_responder_last_seen", "dhcp_observed_responder", ["last_seen_at"]
    )

    op.create_table(
        "dhcp_responder_allowlist",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("server_identifier", sa.String(length=64), nullable=True),
        sa.Column("source_ip", postgresql.INET(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("modified_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_dhcp_responder_allowlist_group", "dhcp_responder_allowlist", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_dhcp_responder_allowlist_group", table_name="dhcp_responder_allowlist")
    op.drop_table("dhcp_responder_allowlist")
    op.drop_index("ix_dhcp_observed_responder_last_seen", table_name="dhcp_observed_responder")
    op.drop_index("ix_dhcp_observed_responder_group", table_name="dhcp_observed_responder")
    op.drop_table("dhcp_observed_responder")
