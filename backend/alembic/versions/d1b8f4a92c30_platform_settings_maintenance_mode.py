"""System-wide maintenance mode — platform_settings maintenance_* columns.

Issue #57. When ``maintenance_mode_enabled`` is true the API 503s every
mutating request (POST/PUT/PATCH/DELETE) outside the exempt allow-list,
with a superadmin bypass — the whole platform goes read-only. The
``maintenance_message`` is surfaced in the global banner + the 503 body;
``maintenance_started_at`` is server-stamped on enable / cleared on
disable. Additive, defaults false / '' → no-op on every existing install.

Revision ID: d1b8f4a92c30
Revises: c7a3e1f90d24
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "d1b8f4a92c30"
down_revision: str | None = "c7a3e1f90d24"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "maintenance_mode_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "maintenance_message",
            sa.String(length=500),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "maintenance_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "maintenance_started_at")
    op.drop_column("platform_settings", "maintenance_message")
    op.drop_column("platform_settings", "maintenance_mode_enabled")
