"""add platform_settings

Revision ID: 3b7f92c10d5e
Revises: 1ad1e7de9fc4
Create Date: 2026-04-13 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3b7f92c10d5e'
down_revision: Union[str, None] = '1ad1e7de9fc4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'platform_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('app_title', sa.String(length=255), nullable=False, server_default='SpatiumDDI'),
        sa.Column('ip_allocation_strategy', sa.String(length=20), nullable=False, server_default='sequential'),
        sa.Column('session_timeout_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('auto_logout_minutes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('utilization_warn_threshold', sa.Integer(), nullable=False, server_default='80'),
        sa.Column('utilization_critical_threshold', sa.Integer(), nullable=False, server_default='95'),
        sa.Column('subnet_tree_default_expanded_depth', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('discovery_scan_enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('discovery_scan_interval_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('github_release_check_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('platform_settings')
