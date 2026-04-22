"""Per-server metric sample rollup tables for DNS + DHCP.

Revision ID: bd4f2a91c7e3
Revises: ac3e1f0d8b42
Create Date: 2026-04-22 09:00:00

Two new tables hold agent-reported counter deltas bucketed into
fixed time windows (default 60 s). Keeping metrics in Postgres
alongside the rest of the model means the built-in dashboard can
render charts without an external Prometheus / InfluxDB stack, which
matches SpatiumDDI's "no external observability tooling required"
stance. Retention is enforced by a nightly Celery task.

Data is write-mostly from agent POSTs and read-light from the
dashboard; the compound PK on ``(server_id, bucket_at)`` is the only
index both paths need.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "bd4f2a91c7e3"
down_revision: str | None = "ac3e1f0d8b42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dns_metric_sample",
        sa.Column("server_id", sa.UUID(), nullable=False),
        sa.Column("bucket_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queries_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("noerror", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("nxdomain", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("servfail", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("recursion", sa.BigInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("server_id", "bucket_at", name="pk_dns_metric_sample"),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["dns_server.id"],
            ondelete="CASCADE",
            name="fk_dns_metric_sample_server",
        ),
    )
    op.create_index(
        "ix_dns_metric_sample_bucket_at",
        "dns_metric_sample",
        ["bucket_at"],
    )

    op.create_table(
        "dhcp_metric_sample",
        sa.Column("server_id", sa.UUID(), nullable=False),
        sa.Column("bucket_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("discover", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("offer", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("request", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ack", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("nak", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("decline", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("release", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("inform", sa.BigInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("server_id", "bucket_at", name="pk_dhcp_metric_sample"),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["dhcp_server.id"],
            ondelete="CASCADE",
            name="fk_dhcp_metric_sample_server",
        ),
    )
    op.create_index(
        "ix_dhcp_metric_sample_bucket_at",
        "dhcp_metric_sample",
        ["bucket_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dhcp_metric_sample_bucket_at", table_name="dhcp_metric_sample")
    op.drop_table("dhcp_metric_sample")
    op.drop_index("ix_dns_metric_sample_bucket_at", table_name="dns_metric_sample")
    op.drop_table("dns_metric_sample")
