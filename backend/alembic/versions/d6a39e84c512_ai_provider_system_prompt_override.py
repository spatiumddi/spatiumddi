"""ai_provider system_prompt_override column.

Per-provider Operator Copilot system-prompt override. NULL means
"use the baked-in default from app.services.ai.chat".
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d6a39e84c512"
down_revision = "c4f7e92d3a18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_provider",
        sa.Column("system_prompt_override", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_provider", "system_prompt_override")
