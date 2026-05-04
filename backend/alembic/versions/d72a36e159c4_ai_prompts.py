"""AI prompts library (issue #90 Phase 2 — custom prompts).

Revision ID: d72a36e159c4
Revises: c8e3a7f10b54
Create Date: 2026-05-04 21:00:00.000000

Operator-curated reusable prompts that can be loaded into the chat
drawer with one click. Two visibility modes per row:

* ``is_shared = True`` — visible to every user with chat access. Created
  / edited by superadmins (so well-known triage prompts can be curated
  centrally).
* ``is_shared = False`` — visible only to ``created_by_user_id``. Lets
  power users keep their own private prompt library without leaking
  half-finished drafts to the team.

Both modes live in the same row type to keep the picker query simple
(one ``OR`` predicate at list time).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "d72a36e159c4"
down_revision: str | None = "c8e3a7f10b54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_prompt",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column(
            "is_shared",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
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
    )
    # Two prompts can share a name as long as one is private + the
    # other shared — the ``ix`` partial-unique below covers the shared
    # case so curated names are unambiguous to every operator.
    op.create_index(
        "uq_ai_prompt_shared_name",
        "ai_prompt",
        ["name"],
        unique=True,
        postgresql_where=sa.text("is_shared = true"),
    )
    # Per-user uniqueness for private prompts so an operator can't
    # accidentally save two "draft" rows under the same name.
    op.create_index(
        "uq_ai_prompt_private_name_per_user",
        "ai_prompt",
        ["created_by_user_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_shared = false"),
    )
    # Hot path: list-by-user (own private + every shared).
    op.create_index(
        "ix_ai_prompt_visibility",
        "ai_prompt",
        ["is_shared", "created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_prompt_visibility", table_name="ai_prompt")
    op.drop_index("uq_ai_prompt_private_name_per_user", table_name="ai_prompt")
    op.drop_index("uq_ai_prompt_shared_name", table_name="ai_prompt")
    op.drop_table("ai_prompt")
