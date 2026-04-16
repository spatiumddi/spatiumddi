"""Add DNS fields to ip_space

Revision ID: e7f3a1c9b5d8
Revises: d4a8b2e6f193
Create Date: 2026-04-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "e7f3a1c9b5d8"
down_revision = "d4a8b2e6f193"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ip_space", sa.Column("dns_group_ids", JSONB(), nullable=True))
    op.add_column("ip_space", sa.Column("dns_zone_id", sa.Text(), nullable=True))
    op.add_column("ip_space", sa.Column("dns_additional_zone_ids", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("ip_space", "dns_additional_zone_ids")
    op.drop_column("ip_space", "dns_zone_id")
    op.drop_column("ip_space", "dns_group_ids")
