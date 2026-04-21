"""DNS zone color — curated swatch key for visual distinction.

Revision ID: f4a9c1b2d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-04-20 12:00:00

Adds a nullable ``color`` column on ``dns_zone`` holding one of the
curated swatch keys (slate/red/amber/emerald/cyan/blue/violet/pink) or
NULL for the default. Painted as a small dot in the zone tree and a
left-border stripe on the zone table row. Free-form hex is rejected at
the API layer so both light and dark themes stay legible.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f4a9c1b2d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dns_zone",
        sa.Column("color", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dns_zone", "color")
