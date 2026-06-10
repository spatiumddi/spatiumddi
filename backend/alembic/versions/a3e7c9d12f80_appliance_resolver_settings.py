"""Issue #158 — appliance DNS resolver (systemd-resolved) settings.

Adds the singleton-row resolver-config columns the appliance's
``systemd-resolved`` drop-in (``/etc/systemd/resolved.conf.d/spatiumddi.conf``)
renders from, plus a best-effort applied-state column on the appliance row
the supervisor reports. Per-fleet rollout reuses the same ConfigBundle →
trigger-file plumbing as SNMP (#153) / NTP (#154) / LLDP (#343) / syslog
(#156) / SSH (#157): the schema change here is the source of truth that
every appliance host (local + remote agents) renders the resolved drop-in
from.

* ``resolver_mode`` — ``automatic`` (default) leaves systemd-resolved to
  pick upstream DNS from per-link NetworkManager / DHCP; ``override`` pins
  a global server list (``DNS=``) that wins over the per-link servers.
* ``resolver_servers`` — JSONB list of upstream resolver IPs used in
  ``override`` mode (rendered as ``DNS=``). Default ``[]``.
* ``resolver_fallback_servers`` — JSONB list of fallback resolver IPs
  (rendered as ``FallbackDNS=``). Default ``[]``.
* ``resolver_search_domains`` — JSONB list of DNS search domains
  (rendered as ``Domains=`` after the route-only ``~.`` default).
  Default ``[]``.
* ``resolver_dnssec`` — systemd-resolved ``DNSSEC=`` (``yes`` / ``no`` /
  ``allow-downgrade``). Default ``allow-downgrade``.
* ``resolver_dns_over_tls`` — systemd-resolved ``DNSOverTLS=`` (``yes`` /
  ``opportunistic`` / ``no``). Default ``no``.

Resolver IPs / domains are NOT secrets, so they are stored verbatim (no
Fernet, no redaction), like NTP server hostnames / SSH public keys.

Appliance row:
* ``resolver_status`` — best-effort state the supervisor reports:
  ``override`` (the spatiumddi.conf drop-in is applied) / ``automatic``
  (no drop-in) / ``failed`` (apply error). NULL on non-appliance /
  pre-#158 rows; the heartbeat handler only overwrites when the supervisor
  sends a non-None value. Per-host, like ``ssh_key_count`` /
  ``syslog_forwarding``.

Revision ID: a3e7c9d12f80
Revises: f1c4a90b27d6
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "a3e7c9d12f80"
down_revision: str | None = "f1c4a90b27d6"
branch_labels: str | None = None
depends_on: str | None = None


_SETTINGS_COLUMNS = [
    sa.Column(
        "resolver_mode",
        sa.String(length=16),
        nullable=False,
        server_default=sa.text("'automatic'"),
    ),
    sa.Column(
        "resolver_servers",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "resolver_fallback_servers",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "resolver_search_domains",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "resolver_dnssec",
        sa.String(length=16),
        nullable=False,
        server_default=sa.text("'allow-downgrade'"),
    ),
    sa.Column(
        "resolver_dns_over_tls",
        sa.String(length=16),
        nullable=False,
        server_default=sa.text("'no'"),
    ),
]


def upgrade() -> None:
    for col in _SETTINGS_COLUMNS:
        op.add_column("platform_settings", col.copy())
    op.add_column(
        "appliance",
        sa.Column("resolver_status", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appliance", "resolver_status")
    for col in _SETTINGS_COLUMNS:
        op.drop_column("platform_settings", col.name)
