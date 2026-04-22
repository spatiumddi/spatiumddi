"""Release check state on platform_settings.

Revision ID: e5b21a8f0d94
Revises: d4a18b20e3c7
Create Date: 2026-04-22 19:00:00

Adds the columns the GitHub-release-check task writes on every run:
``latest_version`` (the tag we most recently saw on GitHub),
``update_available`` (pre-computed so the UI doesn't have to repeat the
CalVer comparison), ``latest_release_url`` (so the banner links straight
to the release notes), ``latest_checked_at`` (for the "last checked
<time> ago" tooltip), and ``latest_check_error`` (so the UI can explain
why the check is stale — GitHub rate-limited, DNS down, etc).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e5b21a8f0d94"
down_revision: str | None = "d4a18b20e3c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column("latest_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "update_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column("latest_release_url", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "platform_settings",
        sa.Column("latest_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "platform_settings",
        sa.Column("latest_check_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "latest_check_error")
    op.drop_column("platform_settings", "latest_checked_at")
    op.drop_column("platform_settings", "latest_release_url")
    op.drop_column("platform_settings", "update_available")
    op.drop_column("platform_settings", "latest_version")
