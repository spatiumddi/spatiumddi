"""add dns assignment fields to ipam blocks and subnets

Revision ID: a1b2c3d4e5f6
Revises: 5d2a8f91c4e6
Create Date: 2026-04-14 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a1b2c3d4e5f6"
down_revision = "5d2a8f91c4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ip_block table
    op.add_column("ip_block", sa.Column("dns_group_ids", postgresql.JSONB(), nullable=True))
    op.add_column("ip_block", sa.Column("dns_zone_id", sa.Text(), nullable=True))
    op.add_column("ip_block", sa.Column("dns_additional_zone_ids", postgresql.JSONB(), nullable=True))
    op.add_column(
        "ip_block",
        sa.Column("dns_inherit_settings", sa.Boolean(), nullable=False, server_default="true"),
    )

    # subnet table
    op.add_column("subnet", sa.Column("dns_group_ids", postgresql.JSONB(), nullable=True))
    op.add_column("subnet", sa.Column("dns_zone_id", sa.Text(), nullable=True))
    op.add_column("subnet", sa.Column("dns_additional_zone_ids", postgresql.JSONB(), nullable=True))
    op.add_column(
        "subnet",
        sa.Column("dns_inherit_settings", sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("subnet", "dns_inherit_settings")
    op.drop_column("subnet", "dns_additional_zone_ids")
    op.drop_column("subnet", "dns_zone_id")
    op.drop_column("subnet", "dns_group_ids")
    op.drop_column("ip_block", "dns_inherit_settings")
    op.drop_column("ip_block", "dns_additional_zone_ids")
    op.drop_column("ip_block", "dns_zone_id")
    op.drop_column("ip_block", "dns_group_ids")
