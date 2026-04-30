"""event subscription + outbox tables for typed webhook delivery

Revision ID: 0f83a227b16d
Revises: 30cda233dce9
Create Date: 2026-04-30 21:02:29.469807

Phase 2 of the notifications-and-external-integrations roadmap:
typed-event webhooks with HMAC signing, an outbox-backed delivery
queue, exponential-backoff retry, and a dead-letter state.

Distinct from ``audit_forward_target`` (which fires on every audit
row in a wire format the operator picks). This surface emits
**typed events** — ``subnet.created``, ``ip.allocated``,
``zone.modified`` — shaped for downstream automation.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0f83a227b16d"
down_revision: Union[str, None] = "30cda233dce9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_subscription",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column(
            "event_types",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "timeout_seconds",
            sa.Integer(),
            server_default=sa.text("10"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("8"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_event_subscription_enabled"),
        "event_subscription",
        ["enabled"],
        unique=False,
    )
    op.create_index(
        op.f("ix_event_subscription_name"),
        "event_subscription",
        ["name"],
        unique=True,
    )

    op.create_table(
        "event_outbox",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["event_subscription.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_event_outbox_event_type",
        "event_outbox",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "ix_event_outbox_subscription_id",
        "event_outbox",
        ["subscription_id"],
        unique=False,
    )
    # Partial index — the worker only ever queries pending / failed
    # rows ordered by next_attempt_at, so we don't need a full-table
    # index. Keeps the index small even on a busy outbox.
    op.create_index(
        "ix_event_outbox_due",
        "event_outbox",
        ["state", "next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("state IN ('pending', 'failed')"),
    )


def downgrade() -> None:
    op.drop_index("ix_event_outbox_due", table_name="event_outbox")
    op.drop_index("ix_event_outbox_subscription_id", table_name="event_outbox")
    op.drop_index("ix_event_outbox_event_type", table_name="event_outbox")
    op.drop_table("event_outbox")
    op.drop_index(
        op.f("ix_event_subscription_name"),
        table_name="event_subscription",
    )
    op.drop_index(
        op.f("ix_event_subscription_enabled"),
        table_name="event_subscription",
    )
    op.drop_table("event_subscription")
