"""api_token resource_grants (#374)

Adds a nullable ``resource_grants`` JSONB column to ``api_token`` for per-token
resource-instance binding (a token scoped to one subnet / DNS zone). Empty/NULL
preserves current behaviour (token inherits the owner's full RBAC).

Revision ID: c3f1a7e09d52
Revises: f3b9c1d6a274
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c3f1a7e09d52"
down_revision = "f3b9c1d6a274"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_token",
        sa.Column("resource_grants", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_token", "resource_grants")
