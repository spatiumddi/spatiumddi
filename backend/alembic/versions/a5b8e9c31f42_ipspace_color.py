"""IP Space color — curated swatch key for visual distinction.

Revision ID: a5b8e9c31f42
Revises: f4a9c1b2d6e7
Create Date: 2026-04-20 12:30:00

Mirrors the DNS zone color field landed in f4a9c1b2d6e7. Painted as a
dot in the IPAM space tree. Values validated server-side against the
same swatch set used for zones (see VALID_ZONE_COLORS / VALID_SPACE_COLORS).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a5b8e9c31f42"
down_revision: str | None = "f4a9c1b2d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ip_space",
        sa.Column("color", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ip_space", "color")
