"""AI usage observability + cost caps (issue #90 Wave 4).

Revision ID: c8e3a7f10b54
Revises: b5d9c41e2f80
Create Date: 2026-05-04 19:30:00.000000

Three additions, all opt-in / non-breaking:

* ``ai_chat_message.cost_usd`` — Numeric(12, 6) computed from
  ``tokens_in`` / ``tokens_out`` × the per-model rate sheet at message
  write time. Stored so report queries don't have to recompute. None
  for messages from local providers (Ollama / LM Studio etc.) where
  the cost is operator-supplied compute, not metered $.

* ``platform_settings`` columns:
    - ``ai_per_user_daily_token_cap``: BigInteger nullable. None =
      unlimited. Default unlimited because operators self-host;
      cloud-API users will set this.
    - ``ai_per_user_daily_cost_cap_usd``: Numeric(10, 4) nullable.
      Same semantics.
    - ``ai_pricing_overrides``: JSONB. Operator-set per-model rates
      that override or supplement the in-code rate sheet. Shape:
      ``{"<model_id>": {"input": <usd_per_million>, "output": <...>}}``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c8e3a7f10b54"
down_revision: Union[str, None] = "b5d9c41e2f80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_chat_message",
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
    )
    op.add_column(
        "platform_settings",
        sa.Column("ai_per_user_daily_token_cap", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "ai_per_user_daily_cost_cap_usd", sa.Numeric(10, 4), nullable=True
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "ai_pricing_overrides",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # Index supports the per-user-today cost lookup hot path.
    op.create_index(
        "ix_ai_chat_message_session_created_cost",
        "ai_chat_message",
        ["session_id", "created_at"],
        postgresql_where=sa.text("cost_usd IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_chat_message_session_created_cost", table_name="ai_chat_message"
    )
    op.drop_column("platform_settings", "ai_pricing_overrides")
    op.drop_column("platform_settings", "ai_per_user_daily_cost_cap_usd")
    op.drop_column("platform_settings", "ai_per_user_daily_token_cap")
    op.drop_column("ai_chat_message", "cost_usd")
