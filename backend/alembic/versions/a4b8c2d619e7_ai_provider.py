"""AI provider configuration (issue #90).

Revision ID: a4b8c2d619e7
Revises: f9c1a7e25b83
Create Date: 2026-05-04 16:00:00.000000

Holds operator-configured LLM providers (OpenAI / Ollama / OpenWebUI /
vLLM / Anthropic / Gemini / Azure OpenAI) with Fernet-encrypted API
keys at rest. Mirrors the ``auth_provider`` shape — same priority +
is_enabled + JSONB-config + LargeBinary-secrets pattern.

Future Anthropic / Gemini / Azure drivers reuse this row layout — the
``kind`` column carries the driver discriminator and the JSONB
``options`` carries per-provider tuning (temperature, max_tokens,
top_p, etc.) so adding a new driver doesn't require a schema change.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "a4b8c2d619e7"
down_revision: Union[str, None] = "f9c1a7e25b83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_provider",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        # openai_compat | anthropic | google | azure_openai
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False, server_default=""),
        # Fernet-encrypted API key (or empty for local providers like
        # Ollama that don't require auth). LargeBinary mirrors how
        # AuthProvider stores secrets.
        sa.Column("api_key_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("default_model", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Lower = higher priority (tried first when a request specifies
        # only kind, not a particular provider). Mirrors auth_provider.priority.
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("100"),
        ),
        # Per-provider tuning knobs that don't warrant their own column:
        # temperature, max_tokens, top_p, top_k, request_timeout_seconds, etc.
        sa.Column(
            "options",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("name", name="uq_ai_provider_name"),
        sa.CheckConstraint(
            "kind IN ('openai_compat', 'anthropic', 'google', 'azure_openai')",
            name="ck_ai_provider_kind",
        ),
    )
    op.create_index(
        "ix_ai_provider_enabled_priority",
        "ai_provider",
        ["is_enabled", "priority"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_provider_enabled_priority", table_name="ai_provider")
    op.drop_table("ai_provider")
