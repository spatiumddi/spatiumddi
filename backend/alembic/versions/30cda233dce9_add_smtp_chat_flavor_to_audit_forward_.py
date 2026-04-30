"""add smtp + chat-flavor to audit_forward, notify_smtp to alert

Revision ID: 30cda233dce9
Revises: a92c81d4f5b0
Create Date: 2026-04-30 20:43:57.324761

Phase 1 of the notifications-and-external-integrations roadmap:

* ``audit_forward_target.webhook_flavor`` — picks the JSON shape at
  send time (``generic`` keeps the original raw audit/alert payload;
  ``slack`` / ``teams`` / ``discord`` wraps in the platform's incoming-
  webhook format so chat-channel delivery works without a transformer).
* ``audit_forward_target.smtp_*`` — eight new columns making
  ``kind="smtp"`` a first-class delivery type. Password is Fernet-
  encrypted at rest (``LargeBinary``); the API exposes only a boolean
  ``smtp_password_set`` matching the Fingerbank pattern.
* ``alert_rule.notify_smtp`` + ``alert_event.delivered_smtp`` — mirror
  the existing syslog/webhook channel toggles + delivery receipts.

Server defaults are explicit on every NOT NULL column so the migration
applies cleanly against existing rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "30cda233dce9"
down_revision: Union[str, None] = "a92c81d4f5b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── audit_forward_target — webhook flavor ────────────────────────
    op.add_column(
        "audit_forward_target",
        sa.Column(
            "webhook_flavor",
            sa.String(length=16),
            server_default=sa.text("'generic'"),
            nullable=False,
        ),
    )

    # ── audit_forward_target — SMTP delivery columns ─────────────────
    op.add_column(
        "audit_forward_target",
        sa.Column(
            "smtp_host",
            sa.String(length=255),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )
    op.add_column(
        "audit_forward_target",
        sa.Column(
            "smtp_port",
            sa.Integer(),
            server_default=sa.text("587"),
            nullable=False,
        ),
    )
    op.add_column(
        "audit_forward_target",
        sa.Column(
            "smtp_security",
            sa.String(length=10),
            server_default=sa.text("'starttls'"),
            nullable=False,
        ),
    )
    op.add_column(
        "audit_forward_target",
        sa.Column(
            "smtp_username",
            sa.String(length=255),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )
    op.add_column(
        "audit_forward_target",
        sa.Column("smtp_password_encrypted", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "audit_forward_target",
        sa.Column(
            "smtp_from_address",
            sa.String(length=320),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )
    op.add_column(
        "audit_forward_target",
        sa.Column(
            "smtp_to_addresses",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "audit_forward_target",
        sa.Column(
            "smtp_reply_to",
            sa.String(length=320),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )

    # ── alert_rule + alert_event — SMTP channel ──────────────────────
    op.add_column(
        "alert_rule",
        sa.Column(
            "notify_smtp",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "alert_event",
        sa.Column(
            "delivered_smtp",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("alert_event", "delivered_smtp")
    op.drop_column("alert_rule", "notify_smtp")
    op.drop_column("audit_forward_target", "smtp_reply_to")
    op.drop_column("audit_forward_target", "smtp_to_addresses")
    op.drop_column("audit_forward_target", "smtp_from_address")
    op.drop_column("audit_forward_target", "smtp_password_encrypted")
    op.drop_column("audit_forward_target", "smtp_username")
    op.drop_column("audit_forward_target", "smtp_security")
    op.drop_column("audit_forward_target", "smtp_port")
    op.drop_column("audit_forward_target", "smtp_host")
    op.drop_column("audit_forward_target", "webhook_flavor")
