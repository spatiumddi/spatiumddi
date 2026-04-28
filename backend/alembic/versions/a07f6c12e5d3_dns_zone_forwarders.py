"""dns_zone forwarders + forward_only

Revision ID: a07f6c12e5d3
Revises: c8e1f04a932d
Create Date: 2026-04-29 14:00:00.000000

Adds two columns on ``dns_zone`` to support per-zone conditional
forwarders. Only meaningful when ``zone_type = 'forward'`` — for other
zone types they're carried as defaults (empty list, true) and ignored
by the renderer.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a07f6c12e5d3"
down_revision: str | None = "c8e1f04a932d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dns_zone",
        sa.Column(
            "forwarders",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "dns_zone",
        sa.Column(
            "forward_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_zone", "forward_only")
    op.drop_column("dns_zone", "forwarders")
