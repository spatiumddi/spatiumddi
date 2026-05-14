"""Issue #153 — SNMP settings on platform_settings (appliance monitoring)

Adds the singleton-row SNMP config columns the appliance's snmpd uses.
Per-fleet rollout reuses the Phase 8f-4 ConfigBundle → trigger-file
plumbing — the schema change here is the source of truth that every
appliance host (local + remote agents) renders ``snmpd.conf`` from.

* ``snmp_enabled`` — master toggle; defaults off so the column add
  doesn't suddenly expose snmpd on every existing appliance.
* ``snmp_version`` — ``v2c`` or ``v3``. v2c default because the
  community-string path is the lowest-friction lab setup.
* ``snmp_community_encrypted`` — Fernet ciphertext bytes (mirror of
  ``fingerbank_api_key_encrypted``). NULL = not configured. Whole row
  is enforced server-side: enabling v2c without a community is a 422.
* ``snmp_v3_users`` — JSONB list of ``{username, auth_protocol,
  auth_pass_enc, priv_protocol, priv_pass_enc}``. The pass fields
  carry Fernet ciphertext as the URL-safe base64 string Fernet
  already emits, so the column itself stays JSON. NULL-empty
  ``auth_pass_enc`` = noAuth, NULL-empty ``priv_pass_enc`` = noPriv.
* ``snmp_allowed_sources`` — list of CIDRs allowed to query. Empty
  list = no one. Rendered into snmpd.conf as repeated com2sec /
  authuser-host gates so snmpd itself enforces the filter.
* ``snmp_sys_contact`` / ``snmp_sys_location`` — straight strings,
  rendered as ``sysContact`` / ``sysLocation`` MIB values.

Revision ID: b95e2d71f042
Revises: a72f4c89e15d
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b95e2d71f042"
down_revision: str | None = "a72f4c89e15d"
branch_labels: str | None = None
depends_on: str | None = None


_NEW_COLUMNS = [
    sa.Column(
        "snmp_enabled",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    ),
    sa.Column(
        "snmp_version",
        sa.String(length=8),
        nullable=False,
        server_default=sa.text("'v2c'"),
    ),
    sa.Column(
        "snmp_community_encrypted",
        sa.LargeBinary(),
        nullable=True,
    ),
    sa.Column(
        "snmp_v3_users",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "snmp_allowed_sources",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "snmp_sys_contact",
        sa.String(length=255),
        nullable=False,
        server_default=sa.text("''"),
    ),
    sa.Column(
        "snmp_sys_location",
        sa.String(length=255),
        nullable=False,
        server_default=sa.text("''"),
    ),
]


def upgrade() -> None:
    for col in _NEW_COLUMNS:
        op.add_column("platform_settings", col.copy())


def downgrade() -> None:
    for col in _NEW_COLUMNS:
        op.drop_column("platform_settings", col.name)
