"""Multi-target audit forwarding with pluggable formats.

Revision ID: c7e2f5a91d48
Revises: bd4f2a91c7e3
Create Date: 2026-04-22 14:30:00

Replaces the single syslog + single webhook slot on ``PlatformSettings``
with a dedicated ``audit_forward_target`` table. Each row is one
delivery destination — a syslog endpoint (UDP / TCP / TLS) or a generic
HTTPS webhook — with an independent output format and an optional
severity / resource-type filter.

Data preservation: if the existing flat columns carried a configured
syslog or webhook target, seed one row per kind at migration time so
the operator's existing setup keeps working. The flat columns stay on
``platform_settings`` for one release as a fallback; they'll go away
in a follow-up migration once the UI path is well-worn.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7e2f5a91d48"
down_revision: str | None = "bd4f2a91c7e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_forward_target",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "format",
            sa.String(length=32),
            nullable=False,
            server_default="rfc5424_json",
        ),
        # syslog-specific
        sa.Column("host", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("port", sa.Integer(), nullable=False, server_default="514"),
        sa.Column("protocol", sa.String(length=10), nullable=False, server_default="udp"),
        sa.Column("facility", sa.Integer(), nullable=False, server_default="16"),
        sa.Column("ca_cert_pem", sa.Text(), nullable=True),
        # webhook-specific
        sa.Column("url", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column(
            "auth_header",
            sa.String(length=1024),
            nullable=False,
            server_default="",
        ),
        # shared filter (null = no filter)
        sa.Column("min_severity", sa.String(length=16), nullable=True),
        sa.Column(
            "resource_types",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_audit_forward_target_name",
        "audit_forward_target",
        ["name"],
        unique=True,
    )
    op.create_index(
        "ix_audit_forward_target_enabled",
        "audit_forward_target",
        ["enabled"],
    )

    # Seed one row per previously configured target, preserving operator
    # intent through the upgrade. We read the live columns off
    # platform_settings rather than hardcoding defaults.
    conn = op.get_bind()
    res = conn.execute(
        sa.text(
            """
            SELECT audit_forward_syslog_enabled, audit_forward_syslog_host,
                   audit_forward_syslog_port, audit_forward_syslog_protocol,
                   audit_forward_syslog_facility,
                   audit_forward_webhook_enabled, audit_forward_webhook_url,
                   audit_forward_webhook_auth_header
              FROM platform_settings
             LIMIT 1
            """
        )
    ).fetchone()
    if res is not None:
        (
            syslog_enabled,
            syslog_host,
            syslog_port,
            syslog_protocol,
            syslog_facility,
            webhook_enabled,
            webhook_url,
            webhook_auth,
        ) = res
        if syslog_host:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO audit_forward_target
                        (name, enabled, kind, format, host, port, protocol, facility)
                    VALUES
                        (:name, :enabled, 'syslog', 'rfc5424_json',
                         :host, :port, :protocol, :facility)
                    """
                ),
                {
                    "name": "Legacy Syslog",
                    "enabled": bool(syslog_enabled),
                    "host": syslog_host,
                    "port": int(syslog_port or 514),
                    "protocol": syslog_protocol or "udp",
                    "facility": int(syslog_facility or 16),
                },
            )
        if webhook_url:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO audit_forward_target
                        (name, enabled, kind, format, url, auth_header)
                    VALUES
                        (:name, :enabled, 'webhook', 'json_lines',
                         :url, :auth_header)
                    """
                ),
                {
                    "name": "Legacy Webhook",
                    "enabled": bool(webhook_enabled),
                    "url": webhook_url,
                    "auth_header": webhook_auth or "",
                },
            )


def downgrade() -> None:
    op.drop_index("ix_audit_forward_target_enabled", table_name="audit_forward_target")
    op.drop_index("ix_audit_forward_target_name", table_name="audit_forward_target")
    op.drop_table("audit_forward_target")
