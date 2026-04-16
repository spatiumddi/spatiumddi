"""add DHCP default options to platform_settings

Revision ID: fe6715916c27
Revises: d9a4c3b7e812
Create Date: 2026-04-15 21:45:40.150171

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'fe6715916c27'
down_revision: Union[str, None] = 'd9a4c3b7e812'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'platform_settings',
        sa.Column('dhcp_default_dns_servers', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        'platform_settings',
        sa.Column('dhcp_default_domain_name', sa.String(length=255),
                  nullable=False, server_default=''),
    )
    op.add_column(
        'platform_settings',
        sa.Column('dhcp_default_domain_search', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        'platform_settings',
        sa.Column('dhcp_default_ntp_servers', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        'platform_settings',
        sa.Column('dhcp_default_lease_time', sa.Integer(),
                  nullable=False, server_default='86400'),
    )


def downgrade() -> None:
    op.drop_column('platform_settings', 'dhcp_default_lease_time')
    op.drop_column('platform_settings', 'dhcp_default_ntp_servers')
    op.drop_column('platform_settings', 'dhcp_default_domain_search')
    op.drop_column('platform_settings', 'dhcp_default_domain_name')
    op.drop_column('platform_settings', 'dhcp_default_dns_servers')
