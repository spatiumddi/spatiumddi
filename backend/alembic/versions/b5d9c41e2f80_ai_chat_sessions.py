"""AI chat sessions + messages (issue #90 Wave 3).

Revision ID: b5d9c41e2f80
Revises: a4b8c2d619e7
Create Date: 2026-05-04 18:00:00.000000

Per-user conversation history with the Operator Copilot.

* ``ai_chat_session`` — one row per conversation. Snapshots the
  provider + model + system prompt at session start so a session
  remains coherent if the operator later edits the global config.
* ``ai_chat_message`` — append-only message log. ``role`` mirrors the
  OpenAI chat schema (system / user / assistant / tool). Assistant
  messages that requested tool execution carry a ``tool_calls``
  JSONB array; ``tool`` messages carry the matching ``tool_call_id``.

Token / cost columns land here in Wave 3 but stay None until Wave 4
wires the rate-sheet + per-call accounting.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "b5d9c41e2f80"
down_revision: Union[str, None] = "a4b8c2d619e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_chat_session",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        # Operator-visible label. Auto-generated from the first user
        # message ("Untitled" until the model has anything to summarise).
        sa.Column("name", sa.String(255), nullable=False, server_default="Untitled"),
        # Snapshot of the provider/model used for this session.
        sa.Column("provider_id", UUID(as_uuid=True), nullable=True),
        sa.Column("model", sa.String(255), nullable=False, server_default=""),
        # Snapshot of the system prompt at session start. Stored so a
        # later edit of the global system prompt doesn't retroactively
        # rewrite this conversation's behaviour.
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
        # Soft-archive instead of delete so the operator can recover.
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_ai_chat_session_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["ai_provider.id"],
            name="fk_ai_chat_session_provider",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_ai_chat_session_user_modified",
        "ai_chat_session",
        ["user_id", "modified_at"],
    )

    op.create_table(
        "ai_chat_message",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        # role: system | user | assistant | tool
        sa.Column("role", sa.String(20), nullable=False),
        # Visible content for system/user/assistant messages.
        # For tool messages, the JSON-stringified tool result.
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        # Set on assistant messages that requested tool execution.
        # Shape: [{"id": str, "name": str, "arguments": str (json)}, ...]
        sa.Column("tool_calls", JSONB(), nullable=True),
        # Set on role=tool messages — correlates the result back to
        # the assistant's tool_call entry.
        sa.Column("tool_call_id", sa.String(128), nullable=True),
        # Tool name on role=tool messages (denormalized for cheap UI rendering).
        sa.Column("name", sa.String(128), nullable=True),
        # Per-message LLM-call observability. None until Wave 4 wires
        # the rate-sheet + accounting layer.
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["ai_chat_session.id"],
            name="fk_ai_chat_message_session",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_ai_chat_message_role",
        ),
    )
    op.create_index(
        "ix_ai_chat_message_session_created",
        "ai_chat_message",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_chat_message_session_created", table_name="ai_chat_message"
    )
    op.drop_table("ai_chat_message")
    op.drop_index(
        "ix_ai_chat_session_user_modified", table_name="ai_chat_session"
    )
    op.drop_table("ai_chat_session")
