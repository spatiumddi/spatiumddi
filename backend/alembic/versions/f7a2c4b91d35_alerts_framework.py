"""Alerts framework — AlertRule + AlertEvent tables.

Revision ID: f7a2c4b91d35
Revises: e5c1b3d8f29a
Create Date: 2026-04-21 15:00:00

Two tables:
  * alert_rule — operator-authored rule definitions
  * alert_event — one row per firing; resolved_at NULL = still open

Plus a partial index on alert_event (rule_id, subject_type, subject_id)
WHERE resolved_at IS NULL so the evaluator's "is there already an open
event?" lookup is O(1) even with millions of resolved rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f7a2c4b91d35"
down_revision: str | None = "e5c1b3d8f29a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_rule",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("rule_type", sa.String(length=40), nullable=False),
        sa.Column("threshold_percent", sa.Integer(), nullable=True),
        sa.Column("server_type", sa.String(length=10), nullable=True),
        sa.Column("severity", sa.String(length=10), nullable=False, server_default="warning"),
        sa.Column("notify_syslog", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("notify_webhook", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_alert_rule_name", "alert_rule", ["name"])
    op.create_index("ix_alert_rule_rule_type_enabled", "alert_rule", ["rule_type", "enabled"])

    op.create_table(
        "alert_event",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "rule_id",
            sa.UUID(),
            sa.ForeignKey("alert_rule.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column(
            "subject_display",
            sa.String(length=500),
            nullable=False,
            server_default="",
        ),
        sa.Column("severity", sa.String(length=10), nullable=False, server_default="warning"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "delivered_syslog", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "delivered_webhook", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_alert_event_rule_id", "alert_event", ["rule_id"])
    op.create_index("ix_alert_event_fired_at", "alert_event", ["fired_at"])
    op.create_index(
        "ix_alert_event_rule_subject_open",
        "alert_event",
        ["rule_id", "subject_type", "subject_id"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_alert_event_rule_subject_open", table_name="alert_event")
    op.drop_index("ix_alert_event_fired_at", table_name="alert_event")
    op.drop_index("ix_alert_event_rule_id", table_name="alert_event")
    op.drop_table("alert_event")
    op.drop_index("ix_alert_rule_rule_type_enabled", table_name="alert_rule")
    op.drop_index("ix_alert_rule_name", table_name="alert_rule")
    op.drop_table("alert_rule")
