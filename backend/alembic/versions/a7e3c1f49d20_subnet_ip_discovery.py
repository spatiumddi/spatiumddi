"""subnet IP discovery — opt-in ping/ARP sweep columns (#23)

Revision ID: a7e3c1f49d20
Revises: c2e6a89d4b15
Create Date: 2026-05-29 14:20:00.000000

Per-subnet opt-in for the scheduled IP-discovery sweep (issue #23).
Additive only — every column ships a server_default so existing rows
backfill to "discovery off" and the migration is expand-safe for a
rolling upgrade.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7e3c1f49d20"
down_revision: Union[str, None] = "c2e6a89d4b15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "subnet",
        sa.Column(
            "discovery_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "subnet",
        sa.Column(
            "discovery_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default="360",
        ),
    )
    op.add_column(
        "subnet",
        sa.Column(
            "last_discovery_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("subnet", "last_discovery_at")
    op.drop_column("subnet", "discovery_interval_minutes")
    op.drop_column("subnet", "discovery_enabled")
