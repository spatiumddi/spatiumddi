"""Issue #156 — rsyslog forwarding settings on platform_settings + status on appliance.

Adds the singleton-row syslog-forwarding config columns the appliance's
rsyslog renders from, plus a best-effort status column on the appliance row
the supervisor reports. Per-fleet rollout reuses the same ConfigBundle →
trigger-file plumbing as SNMP (#153) / NTP (#154) / LLDP (#343): the schema
change here is the source of truth that every appliance host (local + remote
agents) renders ``/etc/rsyslog.d/50-spatium-forward.conf`` from.

* ``syslog_enabled`` — master toggle; defaults off so the column add doesn't
  suddenly start shipping logs off every existing appliance.
* ``syslog_targets`` — JSONB list of forward destinations. Each entry is
  ``{host, port, protocol, format, ca_cert_pem}``; ``ca_cert_pem`` carries
  Fernet ciphertext as the URL-safe-base64 string Fernet emits (so the column
  stays JSON-friendly), required only when ``protocol == 'tls'``.
* ``syslog_filter`` — an rsyslog selector (e.g. ``*.*`` or ``authpriv.*``)
  prepended to each ``omfwd`` action. Empty = the renderer defaults to ``*.*``.
* ``syslog_buffer_disk`` — when True, each forward action uses a disk-assisted
  in-memory queue so a brief collector outage doesn't drop logs.

Appliance row:
* ``syslog_forwarding`` — best-effort status the supervisor reports from
  ``systemctl is-active rsyslog`` + config-applied state. One of ``forwarding``
  / ``unreachable`` / ``disabled``; NULL on non-appliance / pre-#156 rows.

Revision ID: e7a3f1c0d294
Revises: d5e9b2c14a07
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "e7a3f1c0d294"
down_revision: str | None = "d5e9b2c14a07"
branch_labels: str | None = None
depends_on: str | None = None


_SETTINGS_COLUMNS = [
    sa.Column(
        "syslog_enabled",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    ),
    sa.Column(
        "syslog_targets",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "syslog_filter",
        sa.String(),
        nullable=False,
        server_default=sa.text("''"),
    ),
    sa.Column(
        "syslog_buffer_disk",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    ),
]


def upgrade() -> None:
    for col in _SETTINGS_COLUMNS:
        op.add_column("platform_settings", col.copy())
    op.add_column(
        "appliance",
        sa.Column("syslog_forwarding", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "syslog_forwarding")
    for col in _SETTINGS_COLUMNS:
        op.drop_column("platform_settings", col.name)
