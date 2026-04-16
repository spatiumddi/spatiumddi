"""add IPAM↔DNS auto-sync settings to platform_settings

Revision ID: d6e1a4b9f082
Revises: c4d9a1e6f827
Create Date: 2026-04-16 22:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d6e1a4b9f082"
down_revision: str | None = "c4d9a1e6f827"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "dns_auto_sync_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dns_auto_sync_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default="60",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dns_auto_sync_delete_stale",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dns_auto_sync_last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "dns_auto_sync_last_run_at")
    op.drop_column("platform_settings", "dns_auto_sync_delete_stale")
    op.drop_column("platform_settings", "dns_auto_sync_interval_minutes")
    op.drop_column("platform_settings", "dns_auto_sync_enabled")
