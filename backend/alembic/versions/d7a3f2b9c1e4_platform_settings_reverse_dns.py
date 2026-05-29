"""platform settings — reverse-DNS auto-population (#41)

Revision ID: d7a3f2b9c1e4
Revises: a7e3c1f49d20
Create Date: 2026-05-29 19:30:00.000000

Adds the four ``reverse_dns_*`` columns that drive the scheduled
reverse-DNS (PTR) auto-population sweep (issue #41). Additive only —
every column ships a server_default so existing rows backfill to
"reverse-DNS off" and the migration is expand-safe for a rolling
upgrade. ``reverse_dns_resolvers`` is a JSONB list of resolver IPs
(NULL / empty = use the worker's system resolvers).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7a3f2b9c1e4"
down_revision: Union[str, None] = "a7e3c1f49d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "reverse_dns_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "reverse_dns_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default="360",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "reverse_dns_resolvers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "reverse_dns_last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "reverse_dns_last_run_at")
    op.drop_column("platform_settings", "reverse_dns_resolvers")
    op.drop_column("platform_settings", "reverse_dns_interval_minutes")
    op.drop_column("platform_settings", "reverse_dns_enabled")
