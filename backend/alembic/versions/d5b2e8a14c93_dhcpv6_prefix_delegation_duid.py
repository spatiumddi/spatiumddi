"""DHCPv6 prefix delegation + DUID reservations (#368)

Adds pd-pool columns to ``dhcp_pool`` (pd_prefix / delegated_length /
excluded_prefix, used only for pool_type='pd') and a ``duid`` column to
``dhcp_static_assignment`` for DHCPv6 DUID-keyed host reservations. All
nullable and additive — no behaviour change for existing v4 rows.

Revision ID: d5b2e8a14c93
Revises: c3f1a7e09d52
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d5b2e8a14c93"
down_revision = "c3f1a7e09d52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dhcp_pool", sa.Column("pd_prefix", sa.String(length=64), nullable=True))
    op.add_column("dhcp_pool", sa.Column("delegated_length", sa.Integer(), nullable=True))
    op.add_column("dhcp_pool", sa.Column("excluded_prefix", sa.String(length=64), nullable=True))
    op.add_column("dhcp_static_assignment", sa.Column("duid", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("dhcp_static_assignment", "duid")
    op.drop_column("dhcp_pool", "excluded_prefix")
    op.drop_column("dhcp_pool", "delegated_length")
    op.drop_column("dhcp_pool", "pd_prefix")
