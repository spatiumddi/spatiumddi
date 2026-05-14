"""Issue #170 Wave A2 — Appliance supervisor identity + register flow.

Lands the schema half of the supervisor registration story:

* ``appliance`` table — one row per supervisor that's claimed a pairing
  code. Carries the supervisor's Ed25519 public key + identity
  metadata. State machine starts at ``pending_approval``; admin
  approval (Wave B1) flips it to ``approved`` and triggers cert
  signing. Approve / reject / delete are all DELETEs of this row —
  rejecting a pending appliance and deleting an approved one share
  the same cleanup path on the supervisor side (it sees its
  ``appliance_id`` go 404 on next poll and re-bootstraps).
* ``platform_settings.supervisor_registration_enabled`` — feature flag
  defaulting to FALSE. The new ``POST /api/v1/appliance/supervisor/
  register`` endpoint 404s while disabled so existing dns / dhcp
  agent installs (which still use ``/dns/agents/register`` /
  ``/dhcp/agents/register`` + the long PSK) aren't affected by Wave A
  shipping. Operators flip the flag when they're ready to try the
  new supervisor path.

The public-key fingerprint is sha256(public_key_der), hex-encoded
(64 chars). UNIQUE so a duplicate-key claim from a re-registering
supervisor short-circuits to "you already exist" instead of creating
a phantom second row.

``paired_via_code_id`` is ON DELETE SET NULL so a Wave A3 pairing-code
sweep that drops old rows doesn't cascade-delete the appliances those
codes provisioned.

Revision ID: a4f9c2e8b316
Revises: f8a92c1d3b65
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "a4f9c2e8b316"
down_revision: str | None = "f8a92c1d3b65"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "appliance",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Operator-supplied hostname from the install wizard. Free-form
        # — used purely for display in the fleet UI + audit lines, so
        # uniqueness is not enforced (two boxes named "dns-east-1"
        # don't break anything; the operator notices and renames one).
        sa.Column("hostname", sa.String(length=255), nullable=False),
        # Raw Ed25519 public key, DER-encoded. Stored bytes-for-bytes
        # as the supervisor submitted them so the upcoming B1 cert
        # signer can re-derive identity material without re-parsing.
        sa.Column("public_key_der", sa.LargeBinary(), nullable=False),
        # sha256(public_key_der) hex-encoded. UNIQUE — a supervisor
        # that resubmits the same pubkey hits the same row and the
        # register endpoint replies "already registered" (idempotent
        # restart-after-crash semantics).
        sa.Column("public_key_fingerprint", sa.String(length=64), nullable=False),
        # Supervisor version string from the register call, e.g.
        # ``"2026.05.14-1"``. Used by the fleet UI's "needs upgrade"
        # banner. NULLable because the very first registration carries
        # whatever the supervisor reports — possibly empty on a hand-
        # rolled supervisor build.
        sa.Column("supervisor_version", sa.String(length=64), nullable=True),
        sa.Column(
            "paired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Source IP that posted the register call. String not INET
        # because the rest of the project follows that convention
        # (see pairing_code.used_by_ip).
        sa.Column("paired_from_ip", sa.String(length=64), nullable=True),
        # Pointer at the pairing_code row that admitted this register.
        # ON DELETE SET NULL — Wave A3's pairing-code reaper sweeps
        # old terminal codes; we don't want those sweeps to take down
        # the appliances they provisioned.
        sa.Column(
            "paired_via_code_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pairing_code.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # ``pending_approval`` | ``approved`` | ``rejected`` —
        # ``rejected`` is reachable in B1 only; A2 only ever writes
        # ``pending_approval`` and B1 transitions to ``approved`` on
        # admin approval.
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending_approval'"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending_approval', 'approved', 'rejected')",
            name="appliance_state_chk",
        ),
    )
    op.create_index(
        "ix_appliance_public_key_fingerprint",
        "appliance",
        ["public_key_fingerprint"],
        unique=True,
    )
    op.create_index(
        "ix_appliance_state",
        "appliance",
        ["state"],
    )

    # Feature flag — supervisor register endpoint is dormant unless
    # this is flipped on. Default FALSE so Wave A landing doesn't
    # change behaviour for any existing install.
    op.add_column(
        "platform_settings",
        sa.Column(
            "supervisor_registration_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "supervisor_registration_enabled")
    op.drop_index("ix_appliance_state", table_name="appliance")
    op.drop_index("ix_appliance_public_key_fingerprint", table_name="appliance")
    op.drop_table("appliance")
