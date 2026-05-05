"""ai_provider enabled_tools allowlist column.

NULL means "all registered tools enabled" — keeps the rollout
backwards-compatible with every existing provider row.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "c4e8b71f0d23"
down_revision = "a8d6e10f3b59"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_provider",
        sa.Column("enabled_tools", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_provider", "enabled_tools")
