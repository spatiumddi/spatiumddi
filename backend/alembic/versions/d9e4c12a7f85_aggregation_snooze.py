"""Aggregation candidate snooze map on PlatformSettings (issue #114).

Revision ID: d9e4c12a7f85
Revises: c3e8b57a2f14
Create Date: 2026-05-06 18:00:00

Stores per-candidate hide entries for the IPAM aggregation badge.
Keys are stable hashes of the parent block + sorted child CIDRs;
values are ISO timestamps (time-bounded snoozes) or the literal
``"permanent"`` (operator-driven "don't suggest again").
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "d9e4c12a7f85"
down_revision: str | None = "c3e8b57a2f14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "aggregation_snooze",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "aggregation_snooze")
