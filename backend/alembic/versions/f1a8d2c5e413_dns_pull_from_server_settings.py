"""dns_pull_from_server platform settings

Scheduled counterpart to ``dns_auto_sync_*`` but for the other direction:
pulls records from each zone's authoritative server (via the driver's
``pull_zone_records`` — AXFR on Windows DNS today) and additively imports
anything missing from SpatiumDDI's DB.

Revision ID: f1a8d2c5e413
Revises: e3c7b91f2a45
Create Date: 2026-04-17 20:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1a8d2c5e413"
down_revision: str | None = "e3c7b91f2a45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "dns_pull_from_server_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dns_pull_from_server_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "dns_pull_from_server_last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "dns_pull_from_server_last_run_at")
    op.drop_column("platform_settings", "dns_pull_from_server_interval_minutes")
    op.drop_column("platform_settings", "dns_pull_from_server_enabled")
