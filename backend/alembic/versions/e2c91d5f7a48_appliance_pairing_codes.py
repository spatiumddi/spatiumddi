"""Issue #169 — Appliance pairing codes (short-lived, single-use)

Operators joining a DNS / DHCP agent appliance to a control plane
currently have to type the long opaque ``DNS_AGENT_KEY`` / ``DHCP_AGENT_KEY``
hex string into the installer wizard. On a fresh appliance install
over a console (Proxmox / IPMI / serial) where copy-paste is awkward,
this fails frequently.

This migration adds the ``pairing_code`` table — a short-lived,
single-use, 8-digit code generated on the control plane that the
agent's installer exchanges for the real bootstrap key via a public
``POST /api/v1/appliance/pair`` endpoint.

Columns:

* ``code_hash`` — sha256 of the 8-digit code. The cleartext code
  is shown exactly once on creation and never persisted. The hash
  is unique so we can look up by-code in O(log n).
* ``deployment_kind`` — ``"dns"`` or ``"dhcp"``; picks which of
  ``DNS_AGENT_KEY`` / ``DHCP_AGENT_KEY`` the consume endpoint hands
  back. ``"agent"`` is reserved for the future generic-agent role
  (#170) and rejected at the API layer for now.
* ``server_group_id`` — optional pre-assignment of the agent to a
  specific server group. Stored as a free-form UUID rather than a FK
  to DNSServerGroup / DHCPServerGroup because the column is
  polymorphic across both. Validation that the UUID resolves to a
  real group of the right kind lives in the create endpoint.
* ``expires_at`` — wall-clock expiry, default 15 min from creation.
* ``used_at`` / ``used_by_ip`` / ``used_by_hostname`` — populated
  atomically on successful consume. Once ``used_at`` is non-null the
  code is dead.
* ``revoked_at`` / ``revoked_by_user_id`` — operator-driven cancel
  before claim.
* ``note`` — free-form operator description ("for dns-west-2").

Indexed on ``code_hash`` (unique) for the consume lookup, and on
``expires_at`` for the reaper sweep.

Revision ID: e2c91d5f7a48
Revises: d4f8c91a2e35
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "e2c91d5f7a48"
down_revision: str | None = "d4f8c91a2e35"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "pairing_code",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # sha256 of the cleartext code, hex-encoded (64 chars). Unique
        # so concurrent code generation can never produce a collision
        # the consume endpoint can't disambiguate; on the (vanishingly
        # rare) collision we surface a 500 and the operator retries.
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        # Last two digits of the cleartext code — surfaced in the list
        # endpoint so an operator who wrote down the code can correlate
        # it back to its row visually. NOT a security gate (2 digits is
        # trivial entropy); just a UX affordance.
        sa.Column("code_last_two", sa.String(length=2), nullable=False),
        sa.Column("deployment_kind", sa.String(length=16), nullable=False),
        # Polymorphic FK target — either dns_server_group.id or
        # dhcp_server_group.id depending on ``deployment_kind``. No DB
        # FK so we don't need a polymorphic-FK trick.
        sa.Column("server_group_id", UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_ip", sa.String(length=64), nullable=True),
        sa.Column("used_by_hostname", sa.String(length=255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "deployment_kind IN ('dns', 'dhcp')",
            name="pairing_code_deployment_kind_chk",
        ),
    )
    op.create_index(
        "ix_pairing_code_code_hash",
        "pairing_code",
        ["code_hash"],
        unique=True,
    )
    op.create_index(
        "ix_pairing_code_expires_at",
        "pairing_code",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pairing_code_expires_at", table_name="pairing_code")
    op.drop_index("ix_pairing_code_code_hash", table_name="pairing_code")
    op.drop_table("pairing_code")
