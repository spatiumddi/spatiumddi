"""Issue #170 Wave C2 — appliance role assignment + multi-role + DHCP network mode.

Lands the schema half of the role-assignment surface. After this
migration the control plane has:

* **``appliance.assigned_roles``** — JSONB list of strings,
  defaults to ``[]``. The operator's chosen subset of
  ``dns-bind9`` / ``dns-powerdns`` / ``dhcp`` / ``observer`` /
  ``custom``. ``dns-bind9`` + ``dns-powerdns`` are mutually
  exclusive (one engine per box); the role-assignment endpoint
  enforces this server-side. Empty list = approved but idle (no
  service containers running).
* **``appliance.assigned_dns_group_id``** — FK to
  ``dns_server_group``. Set when ``assigned_roles`` includes a
  DNS role; the supervisor's heartbeat response carries the
  group's identity + bootstrap key so the dns-bind9 / dns-powerdns
  service container can register normally.
* **``appliance.assigned_dhcp_group_id``** — same shape for DHCP.
* **``appliance.tags``** — JSONB free-form key:value (string)
  pairs for fleet targeting (``site:prod-east``, ``tier:edge``).
  No semantic interpretation — operators bring meaning to them.
  Surfaced by future fleet-UI filters + MCP tools.

* **``dhcp_server_group.network_mode``** — ``host`` (default,
  today's behaviour) or ``bridged``. Drives the supervisor's
  compose rendering for the dhcp-kea service container:
  ``host`` → ``network_mode: host`` (raw L2 broadcasts);
  ``bridged`` → ``ports: ["67:67/udp"]`` (relayed unicast only).
  See the issue's "DHCP networking modes" section.

Revision ID: ee86cea94cf5
Revises: 79a2e409e774
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "ee86cea94cf5"
down_revision: str | None = "79a2e409e774"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── appliance role assignment + tags ───────────────────────────
    op.add_column(
        "appliance",
        sa.Column(
            "assigned_roles",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "assigned_dns_group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dns_server_group.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "assigned_dhcp_group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dhcp_server_group.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "tags",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # ── dhcp_server_group network mode ─────────────────────────────
    op.add_column(
        "dhcp_server_group",
        sa.Column(
            "network_mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'host'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dhcp_server_group", "network_mode")
    op.drop_column("appliance", "tags")
    op.drop_column("appliance", "assigned_dhcp_group_id")
    op.drop_column("appliance", "assigned_dns_group_id")
    op.drop_column("appliance", "assigned_roles")
