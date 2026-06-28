"""block allocated_ips + total_ips cached counts

Adds cached ``allocated_ips`` + ``total_ips`` columns to ``ip_block`` so block
rows can render Used IPs (not just a utilization bar). Backfills both from the
existing data: ``total_ips`` from the CIDR size (clamped to BIGINT for huge
IPv6 blocks), ``allocated_ips`` from the recursive sum of descendant subnets'
allocated_ips — mirroring ``_update_block_utilization``.

Revision ID: b7c2f1a9d4e6
Revises: 30135c361a47
Create Date: 2026-06-28
"""

import sqlalchemy as sa

from alembic import op

revision = "b7c2f1a9d4e6"
down_revision = "30135c361a47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ip_block",
        sa.Column("allocated_ips", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ip_block",
        sa.Column("total_ips", sa.BigInteger(), nullable=False, server_default="0"),
    )

    # total_ips = CIDR size, clamped to BIGINT max (a /16 IPv6 block is 2^112,
    # which overflows BIGINT — it reads as "uncountable" in the UI like a /64).
    op.execute("""
        UPDATE ip_block SET total_ips = LEAST(
            CASE WHEN family(network) = 4
                THEN (2::numeric ^ (32 - masklen(network)))
                ELSE (2::numeric ^ (128 - masklen(network)))
            END,
            9223372036854775807::numeric
        )::bigint
        """)

    # allocated_ips = sum of allocated_ips across every subnet whose block is
    # this block or a descendant. The CTE pairs each block (root) with every
    # node in its subtree; the aggregate sums subnets per root. Blocks with no
    # subnets in their subtree are left at the 0 default. Mirrors the app's
    # _update_block_utilization (which does not filter soft-deleted subnets).
    op.execute("""
        WITH RECURSIVE tree AS (
            SELECT id AS root, id AS node FROM ip_block
            UNION ALL
            SELECT t.root, c.id
            FROM ip_block c JOIN tree t ON c.parent_block_id = t.node
        )
        UPDATE ip_block b
        SET allocated_ips = sub.total
        FROM (
            SELECT t.root, COALESCE(SUM(s.allocated_ips), 0) AS total
            FROM tree t JOIN subnet s ON s.block_id = t.node
            GROUP BY t.root
        ) sub
        WHERE b.id = sub.root
        """)


def downgrade() -> None:
    op.drop_column("ip_block", "total_ips")
    op.drop_column("ip_block", "allocated_ips")
