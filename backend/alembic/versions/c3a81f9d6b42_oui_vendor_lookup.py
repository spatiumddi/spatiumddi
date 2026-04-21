"""OUI vendor lookup — opt-in IEEE prefix database.

Revision ID: c3a81f9d6b42
Revises: b4d291a6c7e8
Create Date: 2026-04-21 10:00:00

Adds:
  * ``oui_vendor`` table — one row per IEEE-assigned 24-bit prefix.
    Populated by the ``app.tasks.oui_update`` Celery task when the
    operator opts in. Starts empty; the first successful run fills it.
  * Three columns on ``platform_settings`` — ``oui_lookup_enabled``,
    ``oui_update_interval_hours`` (default 24), ``oui_last_updated_at``.

Opt-in by default (feature is off unless a superadmin flips the toggle
in Settings → IPAM). When disabled the API list endpoints skip the
join and the daily fetch task is a no-op.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c3a81f9d6b42"
down_revision: str | None = "b4d291a6c7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oui_vendor",
        sa.Column("prefix", sa.String(length=6), primary_key=True),
        sa.Column("vendor_name", sa.String(length=255), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.add_column(
        "platform_settings",
        sa.Column(
            "oui_lookup_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "oui_update_interval_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "oui_last_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "oui_last_updated_at")
    op.drop_column("platform_settings", "oui_update_interval_hours")
    op.drop_column("platform_settings", "oui_lookup_enabled")
    op.drop_table("oui_vendor")
