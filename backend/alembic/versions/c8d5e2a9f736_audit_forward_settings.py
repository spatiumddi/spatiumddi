"""Audit event forwarding settings — syslog + webhook.

Revision ID: c8d5e2a9f736
Revises: b2f7e91d3c48
Create Date: 2026-04-21 10:00:00

Adds the platform-settings fields for external audit-event forwarding.
Both syslog (RFC 5424 over UDP / TCP) and generic HTTP webhook delivery
are supported and independently toggle-able. Every successful
``AuditLog`` commit is dispatched to the enabled target(s) via the
SQLAlchemy ``after_commit`` listener in
``app/services/audit_forward.py``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c8d5e2a9f736"
down_revision: str | None = "b2f7e91d3c48"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "audit_forward_syslog_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "audit_forward_syslog_host",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "audit_forward_syslog_port",
            sa.Integer(),
            nullable=False,
            server_default="514",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "audit_forward_syslog_protocol",
            sa.String(length=10),
            nullable=False,
            server_default="udp",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "audit_forward_syslog_facility",
            sa.Integer(),
            nullable=False,
            server_default="16",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "audit_forward_webhook_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "audit_forward_webhook_url",
            sa.String(length=1024),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "audit_forward_webhook_auth_header",
            sa.String(length=1024),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "audit_forward_webhook_auth_header")
    op.drop_column("platform_settings", "audit_forward_webhook_url")
    op.drop_column("platform_settings", "audit_forward_webhook_enabled")
    op.drop_column("platform_settings", "audit_forward_syslog_facility")
    op.drop_column("platform_settings", "audit_forward_syslog_protocol")
    op.drop_column("platform_settings", "audit_forward_syslog_port")
    op.drop_column("platform_settings", "audit_forward_syslog_host")
    op.drop_column("platform_settings", "audit_forward_syslog_enabled")
