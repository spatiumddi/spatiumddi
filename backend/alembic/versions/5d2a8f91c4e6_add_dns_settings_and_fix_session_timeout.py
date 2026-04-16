"""add dns settings and fix session timeout

Revision ID: 5d2a8f91c4e6
Revises: 4c9e1f82a3b7
Create Date: 2026-04-14 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "5d2a8f91c4e6"
down_revision = "4c9e1f82a3b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column("dns_default_ttl", sa.Integer(), nullable=False, server_default="3600"),
    )
    op.add_column(
        "platform_settings",
        sa.Column("dns_default_zone_type", sa.String(20), nullable=False, server_default="primary"),
    )
    op.add_column(
        "platform_settings",
        sa.Column("dns_default_dnssec_validation", sa.String(20), nullable=False, server_default="auto"),
    )
    op.add_column(
        "platform_settings",
        sa.Column("dns_recursive_by_default", sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "dns_recursive_by_default")
    op.drop_column("platform_settings", "dns_default_dnssec_validation")
    op.drop_column("platform_settings", "dns_default_zone_type")
    op.drop_column("platform_settings", "dns_default_ttl")
