"""dns threat: RPZ policy-hit attribution

Revision ID: b7f24d1e9a35
Revises: a4e91c7b3d82
Create Date: 2026-07-27 23:40:00.000000

Issue #699, fourth and final piece.

We serve 14 curated blocklists as RPZ zones and discarded every hit,
which left "which machine keeps reaching for blocked domains" — the
question that actually identifies an infected host — unanswerable from
data we were already generating.

A separate table from ``dns_query_log_entry`` on purpose: RPZ hits are
worth keeping far longer than raw queries (the query log is 24 h
operator triage; "this host hit the malware feed every day this week"
has to outlive that), the columns genuinely differ (policy action,
trigger type, matching feed), and sharing a table would force the
query log's retention sweep to either drop hits early or keep queries
too long.

``client_ip`` and ``qname`` are nullable because a hit whose log head
did not parse still proves the blocklist fired, and counting it is
better than dropping it — the attribution surfaces filter those rows
out, the totals keep them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import INET, UUID

revision = "b7f24d1e9a35"
down_revision = "a4e91c7b3d82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dns_rpz_hit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("server_id", UUID(as_uuid=True), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_ip", INET(), nullable=True),
        sa.Column("qname", sa.String(length=512), nullable=True),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("policy", sa.String(length=16), nullable=False),
        sa.Column("rpz_zone", sa.String(length=255), nullable=True),
        sa.Column("raw", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.ForeignKeyConstraint(["server_id"], ["dns_server.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dns_rpz_hit_client_ts", "dns_rpz_hit", ["client_ip", "ts"])
    op.create_index("ix_dns_rpz_hit_ts", "dns_rpz_hit", ["ts"])
    op.create_index("ix_dns_rpz_hit_zone_ts", "dns_rpz_hit", ["rpz_zone", "ts"])


def downgrade() -> None:
    op.drop_index("ix_dns_rpz_hit_zone_ts", table_name="dns_rpz_hit")
    op.drop_index("ix_dns_rpz_hit_ts", table_name="dns_rpz_hit")
    op.drop_index("ix_dns_rpz_hit_client_ts", table_name="dns_rpz_hit")
    op.drop_table("dns_rpz_hit")
