"""AI daily digest enable flag (issue #90 Phase 2 — Operator Copilot).

Revision ID: e93b41ad7c5f
Revises: d72a36e159c4
Create Date: 2026-05-04 21:30:00.000000

Adds ``platform_settings.ai_daily_digest_enabled`` (default false) so
operators can opt in to the once-per-day rollup. The Celery beat
schedule fires the digest task at 08:00 UTC unconditionally; the task
itself reads this column and bails when it's false. That way cadence
toggles in the UI don't require restarting beat.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e93b41ad7c5f"
down_revision: str | None = "d72a36e159c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "ai_daily_digest_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "ai_daily_digest_enabled")
