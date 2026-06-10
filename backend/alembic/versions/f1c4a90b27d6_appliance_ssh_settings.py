"""Issue #157 — appliance SSH authorized_keys + sshd hardening settings.

Adds the singleton-row SSH-config columns the appliance's sshd renders from,
plus a best-effort applied-key-count column on the appliance row the
supervisor reports. Per-fleet rollout reuses the same ConfigBundle →
trigger-file plumbing as SNMP (#153) / NTP (#154) / LLDP (#343) / syslog
(#156): the schema change here is the source of truth that every appliance
host (local + remote agents) renders ``~admin/.ssh/authorized_keys`` +
``/etc/ssh/sshd_config.d/spatiumddi.conf`` from.

* ``ssh_authorized_keys`` — JSONB list of ``{name, public_key, comment}``
  entries. Public keys are NOT secrets, so they are stored verbatim (no
  Fernet, no redaction). Default ``[]``.
* ``ssh_password_auth_enabled`` — sshd ``PasswordAuthentication``. Defaults
  TRUE so existing field installs do NOT silently lose password login on
  upgrade. Flipping it to false once keys are present is an explicit
  operator action (guarded server-side + host-side so you can't lock
  yourself out).
* ``ssh_allow_root_login`` — sshd ``PermitRootLogin`` (``yes`` / ``no``).
  Defaults FALSE (``no``) — the appliance ships with root login off.
* ``ssh_port`` — sshd ``Port``. Defaults 22. Server rejects values < 1024
  except 22 (privileged-port floor); the host runner does the real
  bind / in-use check.
* ``ssh_allowed_source_networks`` — JSONB list of CIDRs the host nftables
  drop-in scopes the ssh port to (sshd has no native source filter).
  Empty = open the ssh port unconditionally; the un-removable port-22
  accept floor in the firewall renderer always stays so a bad port change
  can't lock the operator out.

Appliance row:
* ``ssh_key_count`` — best-effort count of authorized_keys lines the
  supervisor's host runner actually applied. NULL on non-appliance /
  pre-#157 rows. Per-host (not a global len()), like ``snmpd_running``.

Revision ID: f1c4a90b27d6
Revises: e7a3f1c0d294
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "f1c4a90b27d6"
down_revision: str | None = "e7a3f1c0d294"
branch_labels: str | None = None
depends_on: str | None = None


_SETTINGS_COLUMNS = [
    sa.Column(
        "ssh_authorized_keys",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "ssh_password_auth_enabled",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("true"),
    ),
    sa.Column(
        "ssh_allow_root_login",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    ),
    sa.Column(
        "ssh_port",
        sa.Integer(),
        nullable=False,
        server_default=sa.text("22"),
    ),
    sa.Column(
        "ssh_allowed_source_networks",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
]


def upgrade() -> None:
    for col in _SETTINGS_COLUMNS:
        op.add_column("platform_settings", col.copy())
    op.add_column(
        "appliance",
        sa.Column("ssh_key_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "ssh_key_count")
    for col in _SETTINGS_COLUMNS:
        op.drop_column("platform_settings", col.name)
