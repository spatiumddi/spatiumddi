"""Issue #170 Wave A3 — Pairing code redesign.

The #169 pairing-code model was tightly coupled to the per-service
bootstrap-key pattern: every code carried a ``deployment_kind`` so the
``POST /api/v1/appliance/pair`` consume endpoint could hand back the
right ``DNS_AGENT_KEY`` / ``DHCP_AGENT_KEY``. The Wave A2 supervisor
identity model removes the long PSK entirely — supervisors prove
identity via Ed25519 public-key submission, not by holding a shared
secret. Pairing codes are therefore kind-agnostic now.

Schema changes:

* **Drop** ``pairing_code.deployment_kind`` + its CHECK constraint.
* **Drop** ``pairing_code.server_group_id`` — group assignment moves
  to the post-approval fleet UI (Wave B+).
* **Drop** ``pairing_code.used_at`` + ``used_by_ip`` +
  ``used_by_hostname`` — claim accounting moves to the new
  ``pairing_claim`` child table, which carries one row per
  successful claim (ephemeral codes: exactly one row; persistent
  codes: many rows, one per supervisor).
* **Add** ``persistent BOOLEAN NOT NULL DEFAULT false`` — when true
  the code can be claimed by N appliances; when false it's
  single-use (today's behaviour).
* **Add** ``enabled BOOLEAN NOT NULL DEFAULT true`` — only meaningful
  for ``persistent=true``; admin can disable a long-lived code
  without deleting it.
* **Add** ``max_claims INTEGER NULL`` — optional ceiling on the
  number of claims a persistent code can accept. NULL = unlimited.
* **Change** ``expires_at`` to NULLable — persistent codes default
  to no expiry; ephemeral codes still require one.

New ``pairing_claim`` table — one row per (code, supervisor)
successful claim. Carries the source IP, hostname, and timestamp,
plus a FK to the ``appliance`` row created by the claim. ON DELETE
CASCADE both ways (deleting a code or an appliance drops the claim
audit; the audit log holds the permanent record).

Revision ID: b5a8d2e9c473
Revises: a4f9c2e8b316
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "b5a8d2e9c473"
down_revision: str | None = "a4f9c2e8b316"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── pairing_code reshape ───────────────────────────────────────
    op.drop_constraint(
        "pairing_code_deployment_kind_chk", "pairing_code", type_="check"
    )
    op.drop_column("pairing_code", "deployment_kind")
    op.drop_column("pairing_code", "server_group_id")
    op.drop_column("pairing_code", "used_at")
    op.drop_column("pairing_code", "used_by_ip")
    op.drop_column("pairing_code", "used_by_hostname")

    op.add_column(
        "pairing_code",
        sa.Column(
            "persistent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "pairing_code",
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "pairing_code",
        sa.Column("max_claims", sa.Integer(), nullable=True),
    )

    # ``expires_at`` becomes nullable — persistent codes can have no
    # expiry; ephemeral codes still require one (enforced at the API
    # layer rather than via a CHECK so admins can later "park" a
    # never-used ephemeral code as a persistent one without DB-level
    # migrations).
    op.alter_column(
        "pairing_code",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )

    # ── pairing_claim ──────────────────────────────────────────────
    op.create_table(
        "pairing_claim",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "pairing_code_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pairing_code.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "appliance_id",
            UUID(as_uuid=True),
            sa.ForeignKey("appliance.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_from_ip", sa.String(length=64), nullable=True),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.UniqueConstraint(
            "pairing_code_id",
            "appliance_id",
            name="pairing_claim_code_appliance_uq",
        ),
    )
    op.create_index(
        "ix_pairing_claim_pairing_code_id",
        "pairing_claim",
        ["pairing_code_id"],
    )
    op.create_index(
        "ix_pairing_claim_appliance_id",
        "pairing_claim",
        ["appliance_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pairing_claim_appliance_id", table_name="pairing_claim")
    op.drop_index("ix_pairing_claim_pairing_code_id", table_name="pairing_claim")
    op.drop_table("pairing_claim")

    op.alter_column(
        "pairing_code",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.drop_column("pairing_code", "max_claims")
    op.drop_column("pairing_code", "enabled")
    op.drop_column("pairing_code", "persistent")

    op.add_column(
        "pairing_code",
        sa.Column("used_by_hostname", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "pairing_code",
        sa.Column("used_by_ip", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "pairing_code",
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "pairing_code",
        sa.Column("server_group_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "pairing_code",
        sa.Column(
            "deployment_kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'dns'"),
        ),
    )
    op.create_check_constraint(
        "pairing_code_deployment_kind_chk",
        "pairing_code",
        "deployment_kind IN ('dns', 'dhcp', 'both')",
    )
