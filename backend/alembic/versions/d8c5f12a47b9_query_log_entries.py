"""Per-server query/activity log entries shipped from agents.

Revision ID: d8c5f12a47b9
Revises: c4e1a87b3920
Create Date: 2026-04-25 17:00:00

Two narrow tables — ``dns_query_log_entry`` and ``dhcp_log_entry``
— hold parsed log lines pushed by BIND9 / Kea agents. Used by the
Logs UI's new "DNS Queries" and "DHCP Activity" tabs. Retention is
operator-configurable via the existing prune-task pattern.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8c5f12a47b9"
down_revision: str | None = "c4e1a87b3920"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dns_query_log_entry",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column(
            "server_id",
            sa.UUID(),
            sa.ForeignKey("dns_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_ip", postgresql.INET(), nullable=True),
        sa.Column("client_port", sa.Integer(), nullable=True),
        sa.Column("qname", sa.String(length=512), nullable=True),
        sa.Column("qclass", sa.String(length=8), nullable=True),
        sa.Column("qtype", sa.String(length=16), nullable=True),
        sa.Column("flags", sa.String(length=64), nullable=True),
        sa.Column("view", sa.String(length=255), nullable=True),
        sa.Column("raw", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dns_query_log_server_ts",
        "dns_query_log_entry",
        ["server_id", "ts"],
    )
    op.create_index("ix_dns_query_log_ts", "dns_query_log_entry", ["ts"])

    op.create_table(
        "dhcp_log_entry",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column(
            "server_id",
            sa.UUID(),
            sa.ForeignKey("dhcp_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column("mac_address", postgresql.MACADDR(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("transaction_id", sa.String(length=32), nullable=True),
        sa.Column("raw", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dhcp_log_server_ts",
        "dhcp_log_entry",
        ["server_id", "ts"],
    )
    op.create_index("ix_dhcp_log_ts", "dhcp_log_entry", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_dhcp_log_ts", table_name="dhcp_log_entry")
    op.drop_index("ix_dhcp_log_server_ts", table_name="dhcp_log_entry")
    op.drop_table("dhcp_log_entry")
    op.drop_index("ix_dns_query_log_ts", table_name="dns_query_log_entry")
    op.drop_index("ix_dns_query_log_server_ts", table_name="dns_query_log_entry")
    op.drop_table("dns_query_log_entry")
