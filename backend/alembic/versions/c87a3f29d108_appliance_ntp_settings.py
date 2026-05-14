"""Issue #154 — NTP settings on platform_settings (appliance chrony config)

Adds the singleton-row NTP config columns the appliance's chrony uses.
Same fleet-rollout shape as Issue #153 (SNMP): operators edit these
fields once, the agent ConfigBundle long-poll fans the rendered
chrony.conf out to every appliance host within ~60 s.

* ``ntp_source_mode`` — ``pool`` / ``servers`` / ``mixed``. ``pool``
  uses the default ``pool.ntp.org`` (cloud-init default), ``servers``
  uses operator-supplied unicast servers, ``mixed`` uses both.
* ``ntp_pool_servers`` — JSONB list of pool hostnames; default
  ``["pool.ntp.org"]``. Empty list with ``source_mode=pool`` means
  no time source (renderer warns).
* ``ntp_custom_servers`` — JSONB list of
  ``{host, iburst: bool, prefer: bool}``. ``iburst`` accelerates
  initial sync; ``prefer`` lets operators tag the canonical source
  among multiples. No secrets — server hostnames are not sensitive,
  no Fernet needed (contrast with SNMP community).
* ``ntp_allow_clients`` — bool. When True, chrony also acts as an
  NTP server (``allow`` lines in chrony.conf) and the host firewall
  opens UDP 123 inbound. Useful for control-plane appliances that
  serve time on isolated networks.
* ``ntp_allow_client_networks`` — JSONB list of CIDRs the appliance
  will serve NTP to. Empty list with ``allow_clients=true`` = nothing
  served (renderer warns). Ignored when ``allow_clients=false``.

Revision ID: c87a3f29d108
Revises: b95e2d71f042
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c87a3f29d108"
down_revision: str | None = "b95e2d71f042"
branch_labels: str | None = None
depends_on: str | None = None


_NEW_COLUMNS = [
    sa.Column(
        "ntp_source_mode",
        sa.String(length=16),
        nullable=False,
        server_default=sa.text("'pool'"),
    ),
    sa.Column(
        "ntp_pool_servers",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[\"pool.ntp.org\"]'::jsonb"),
    ),
    sa.Column(
        "ntp_custom_servers",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "ntp_allow_clients",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    ),
    sa.Column(
        "ntp_allow_client_networks",
        JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
]


def upgrade() -> None:
    for col in _NEW_COLUMNS:
        op.add_column("platform_settings", col.copy())


def downgrade() -> None:
    for col in _NEW_COLUMNS:
        op.drop_column("platform_settings", col.name)
