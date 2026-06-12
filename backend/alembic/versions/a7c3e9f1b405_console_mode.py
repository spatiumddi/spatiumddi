"""appliance console mode — replace verbose_boot with console_mode (#393)

Replaces the boolean ``platform_settings.verbose_boot`` with a
``console_mode`` enum-string so the operator can pick between three
console behaviours instead of a single on/off:

* ``dashboard``         — quiet boot + Talos console dashboard (default)
* ``verbose_dashboard`` — verbose boot output, then the dashboard
* ``text_console``      — verbose boot + a plain getty login (no dashboard)

Backfills the old boolean: ``verbose_boot=True`` → ``text_console``
(the old "standard Linux console" behaviour), ``False`` → ``dashboard``.

Revision ID: a7c3e9f1b405
Revises: f4a1c9e7b2d8
Create Date: 2026-06-12

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c3e9f1b405"
down_revision: str | None = "f4a1c9e7b2d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "console_mode",
            sa.String(length=20),
            nullable=False,
            server_default="dashboard",
        ),
    )
    # Backfill from the old boolean before dropping it.
    op.execute(
        "UPDATE platform_settings "
        "SET console_mode = CASE WHEN verbose_boot THEN 'text_console' "
        "ELSE 'dashboard' END"
    )
    op.drop_column("platform_settings", "verbose_boot")


def downgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "verbose_boot",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        "UPDATE platform_settings "
        "SET verbose_boot = (console_mode <> 'dashboard')"
    )
    op.drop_column("platform_settings", "console_mode")
