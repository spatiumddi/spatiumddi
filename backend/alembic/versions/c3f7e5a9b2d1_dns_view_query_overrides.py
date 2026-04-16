"""Add view-level query control overrides to dns_view.

Revision ID: c3f7e5a9b2d1
Revises: a1b2c3d4e5f6
Create Date: 2026-04-14

Adds ``allow_query`` and ``allow_query_cache`` JSONB columns to ``dns_view`` so
a split-horizon view can override the server-group-level defaults defined in
``dns_server_options``. Columns are nullable; null means "inherit".

Note: ``down_revision`` is set to ``a1b2c3d4e5f6`` (the IPAM DNS-assignment
migration), which was the current head on ``main`` at the time of writing.
Adjust if newer migrations land between generation and apply.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c3f7e5a9b2d1"
down_revision = "c5f2a9b18e34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dns_view",
        sa.Column("allow_query", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "dns_view",
        sa.Column(
            "allow_query_cache",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dns_view", "allow_query_cache")
    op.drop_column("dns_view", "allow_query")
