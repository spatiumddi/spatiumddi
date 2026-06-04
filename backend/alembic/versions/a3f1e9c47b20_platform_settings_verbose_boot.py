"""Appliance verbose-boot console toggle — platform_settings.verbose_boot.

When true, the appliance boots a standard Linux console (drop the loglevel=3
cap + systemd.show_status=1 + spatium-console=off) instead of the quiet boot +
Talos dashboard. Additive, defaults false → no-op on every existing install.

Revision ID: a3f1e9c47b20
Revises: c1e7f3a90b4d
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "a3f1e9c47b20"
down_revision: str | None = "c1e7f3a90b4d"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "verbose_boot",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "verbose_boot")
