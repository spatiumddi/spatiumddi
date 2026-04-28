"""drop dead discovery_scan settings columns

The Settings page exposed ``discovery_scan_enabled`` +
``discovery_scan_interval_minutes`` toggles that only persisted to
``platform_settings`` — no Celery beat task or any other code ever
read them. The actual discovery surface in this product is
SNMP-based (``network_device`` polling with the per-device
``auto_create_discovered`` toggle) plus ARP / FDB cross-reference,
so the dead toggles were just misleading. This migration drops
them entirely. Destructive — the columns hold no operational data
(alpha-stage project, never wired to anything that read them).

Revision ID: a4d92f61c08b
Revises: c4e7a2f813b9
Create Date: 2026-04-28 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4d92f61c08b'
down_revision: Union[str, None] = 'c4e7a2f813b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('platform_settings', 'discovery_scan_interval_minutes')
    op.drop_column('platform_settings', 'discovery_scan_enabled')


def downgrade() -> None:
    op.add_column(
        'platform_settings',
        sa.Column(
            'discovery_scan_enabled',
            sa.Boolean(),
            nullable=False,
            server_default='false',
        ),
    )
    op.add_column(
        'platform_settings',
        sa.Column(
            'discovery_scan_interval_minutes',
            sa.Integer(),
            nullable=False,
            server_default='60',
        ),
    )
