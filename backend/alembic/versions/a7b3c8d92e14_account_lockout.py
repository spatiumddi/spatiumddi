"""account lockout after N failed logins (issue #71)

Adds three per-user counters and three platform-level knobs for
windowed-counter lockout. ``lockout_threshold = 0`` disables the
feature; that's the default so an upgrade never blocks a working
admin out of their own platform.

* ``user.failed_login_count`` — running counter, reset on success.
* ``user.failed_login_locked_until`` — NULL when not locked. While set
  in the future the login handler returns 403 without checking the
  password.
* ``user.last_failed_login_at`` — used by the windowed reset: if the
  newest failure is older than ``lockout_reset_minutes`` we drop the
  counter back to zero before incrementing (so 1 fail every 6 minutes
  never accumulates into a lockout).

Revision ID: a7b3c8d92e14
Revises: f3a8c2d491e7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b3c8d92e14"
down_revision = "f3a8c2d491e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "lockout_threshold",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "lockout_duration_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("15"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "lockout_reset_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("15"),
        ),
    )

    op.add_column(
        "user",
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "failed_login_locked_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "last_failed_login_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "last_failed_login_at")
    op.drop_column("user", "failed_login_locked_until")
    op.drop_column("user", "failed_login_count")
    op.drop_column("platform_settings", "lockout_reset_minutes")
    op.drop_column("platform_settings", "lockout_duration_minutes")
    op.drop_column("platform_settings", "lockout_threshold")
