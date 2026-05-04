"""AI operation proposals (issue #90 Phase 2 — write tools with preview/apply).

Revision ID: f63d9a8e2c47
Revises: e93b41ad7c5f
Create Date: 2026-05-04 22:00:00.000000

When a Copilot write tool fires, we don't execute the mutation. We
persist an ``ai_operation_proposal`` row that captures the args + a
human-readable preview, then surface that token to the operator. The
actual mutation only runs after an explicit POST to
``/api/v1/ai/proposals/{token}/apply``.

A 30-minute TTL bounds how long a stale proposal hangs around — long
enough for a slow review without keeping yesterday's "create subnet"
proposals lying around. The Celery cleanup task drops expired+
unapplied rows every hour. Applied rows persist for audit (operators
want to see "which AI proposal landed"); a separate retention pass
prunes them after 30 days.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "f63d9a8e2c47"
down_revision: str | None = "e93b41ad7c5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_operation_proposal",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ai_chat_session.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column(
            "args",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("preview_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "discarded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # On apply, capture both the dispatch result + any error message
        # so the audit trail tells the full story without a join.
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Hot path: list pending proposals for a session.
    op.create_index(
        "ix_ai_operation_proposal_session",
        "ai_operation_proposal",
        ["session_id", "created_at"],
    )
    # Cleanup pass joins on these.
    op.create_index(
        "ix_ai_operation_proposal_expires_at",
        "ai_operation_proposal",
        ["expires_at"],
        postgresql_where=sa.text("applied_at IS NULL AND discarded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_operation_proposal_expires_at", table_name="ai_operation_proposal"
    )
    op.drop_index("ix_ai_operation_proposal_session", table_name="ai_operation_proposal")
    op.drop_table("ai_operation_proposal")
