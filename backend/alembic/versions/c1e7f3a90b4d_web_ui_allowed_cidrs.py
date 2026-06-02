"""Issue #285 Phase 6 — web_ui_allowed_cidrs on platform_settings.

Source-scope the Web UI (frontend hostPort 80/443 + the MetalLB control-plane
VIP). Empty list = open (the current behaviour), so this is a no-op default on
every existing install — purely additive.

Revision ID: c1e7f3a90b4d
Revises: f5b8d2c91a06
Create Date: 2026-06-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "c1e7f3a90b4d"
down_revision: str | None = "f5b8d2c91a06"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "web_ui_allowed_cidrs",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "web_ui_allowed_cidrs")
