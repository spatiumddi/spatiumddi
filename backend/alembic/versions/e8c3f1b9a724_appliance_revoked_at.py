"""Appliance soft-delete via revoked_at column (#170 Wave E follow-up).

Before this migration ``DELETE /api/v1/appliance/appliances/{id}``
hard-dropped the row. Operators wanted a soft-delete shape: the
appliance is disowned (heartbeats return 403 → supervisor flips to
``revoked`` per the agent-side state machine + tears down its
service containers), but the row sticks around so an admin can
either Re-authorize (clear ``revoked_at`` + set state back to
``approved``) or Permanently delete after they're sure.

Retention is configured via the new ``appliance_revoked_retention_days``
platform setting (default 30); a Celery beat task hard-deletes
revoked rows past the window. The retention column on
PlatformSettings ships in the same migration so a single ``alembic
upgrade`` lands both halves of the feature.

Revision ID: e8c3f1b9a724
Revises: d7f2a91c4b58
Create Date: 2026-05-15 21:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e8c3f1b9a724"
down_revision = "d7f2a91c4b58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appliance",
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "appliance_revoked_retention_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "appliance_revoked_retention_days")
    op.drop_column("appliance", "revoked_at")
