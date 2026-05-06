"""password policy enforcement (issue #70)

Adds configurable password-policy knobs to ``platform_settings`` plus
two per-user columns:

* ``user.password_changed_at`` — timestamp the password was last set.
  Used by the max-age check that flips ``force_password_change`` at
  login.
* ``user.password_history_encrypted`` — Fernet-encrypted JSON list of
  prior bcrypt hashes (most-recent-first), capped to the configured
  ``password_history_count``. NULL means "no history yet" (fresh
  install / external-auth user).

Defaults are intentionally permissive (12 chars + upper/lower/digit, no
symbol requirement, history of 5, no max age) so an upgrade doesn't
suddenly lock anyone out — operators tighten in Settings → Security.

Revision ID: f3a8c2d491e7
Revises: b5d8a3f12c91
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3a8c2d491e7"
down_revision = "b5d8a3f12c91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "password_min_length",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("12"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "password_require_uppercase",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "password_require_lowercase",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "password_require_digit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "password_require_symbol",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "password_history_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("5"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "password_max_age_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.add_column(
        "user",
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "password_history_encrypted",
            sa.LargeBinary(),
            nullable=True,
        ),
    )

    # Backfill ``password_changed_at`` to ``created_at`` on any local-auth
    # user that already has a password — gives the max-age check a sane
    # baseline so a rollout doesn't immediately flag every account as
    # "expired" with default settings (max_age=0 is off, but operators
    # turning it on later still need a backfilled timestamp to age from).
    op.execute(
        """
        UPDATE "user"
        SET password_changed_at = created_at
        WHERE auth_source = 'local'
          AND hashed_password IS NOT NULL
          AND password_changed_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("user", "password_history_encrypted")
    op.drop_column("user", "password_changed_at")
    op.drop_column("platform_settings", "password_max_age_days")
    op.drop_column("platform_settings", "password_history_count")
    op.drop_column("platform_settings", "password_require_symbol")
    op.drop_column("platform_settings", "password_require_digit")
    op.drop_column("platform_settings", "password_require_lowercase")
    op.drop_column("platform_settings", "password_require_uppercase")
    op.drop_column("platform_settings", "password_min_length")
