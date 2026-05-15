"""Issue #170 Wave B1 — Approval workflow + internal CA + capabilities.

Lands the schema half of the approval flow. After this migration the
control plane has:

* A new ``appliance_ca`` table — singleton (id=1) carrying the internal
  CA's RSA-2048 private key (Fernet-encrypted at rest) + the self-
  signed root cert that signs every supervisor's identity cert. The CA
  is generated lazily on first need (first approve attempt) so a
  fresh-install control plane that never approves a supervisor doesn't
  pay the cost.
* New columns on ``appliance``:
  - ``capabilities`` JSONB — the supervisor's advertised facts
    (can_run_dns_bind9 / can_run_dhcp / has_baked_images / cpu_count /
    memory_mb / host_nics / …). Populated on register + every
    heartbeat; the control plane filters role-picker options against it.
  - ``session_token_hash`` VARCHAR(64) — sha256 of the unauth-poll
    token the supervisor uses between register and approval. The
    register response returns the cleartext token once; subsequent
    ``/supervisor/poll`` calls present it for verification. After
    cert issuance the supervisor uses mTLS and the session token is
    cleared.
  - Cert lifecycle columns — ``cert_pem``, ``cert_serial`` (hex),
    ``cert_issued_at``, ``cert_expires_at``. NULL until approval.
  - Approval / rejection columns — ``approved_at`` / ``approved_by_
    user_id`` / ``rejected_at`` / ``rejected_by_user_id``. Reject =
    DELETE the row (supervisor falls back to bootstrapping); these
    columns are reserved for the rare "approve, then later reject /
    re-key" path so the audit trail survives via JSONB on the
    audit_log row. The approved_at column also drives "approved 2 min
    ago / 3 hours ago" UI affordances.

Revision ID: c7e9b3a481f2
Revises: b5a8d2e9c473
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "c7e9b3a481f2"
down_revision: str | None = "b5a8d2e9c473"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── pairing_code follow-up ─────────────────────────────────────
    # The A3 migration's docstring described a ``code_encrypted``
    # column but the matching op.add_column was missing — the model
    # ships it but the DB schema didn't. Catching it up here so the
    # /reveal endpoint's Fernet round-trip works on a fresh install.
    op.add_column(
        "pairing_code",
        sa.Column("code_encrypted", sa.LargeBinary(), nullable=True),
    )

    # ── appliance_ca singleton ─────────────────────────────────────
    op.create_table(
        "appliance_ca",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_cn", sa.String(length=255), nullable=False),
        # RSA-2048 PEM for now — broad client support; we don't need
        # the smaller-cert benefits of ECDSA for an internal CA the
        # supervisor pins explicitly. The supervisor's *identity* key
        # stays Ed25519 (issued under this CA — X.509 supports mixed
        # algorithms for subject vs issuer).
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("cert_pem", sa.Text(), nullable=False),
        sa.Column("key_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="appliance_ca_singleton_chk"),
    )

    # ── appliance column additions ─────────────────────────────────
    op.add_column(
        "appliance",
        sa.Column(
            "capabilities",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "appliance",
        sa.Column("session_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column("appliance", sa.Column("cert_pem", sa.Text(), nullable=True))
    op.add_column(
        "appliance",
        sa.Column("cert_serial", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("cert_issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("cert_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "approved_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "appliance",
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appliance",
        sa.Column(
            "rejected_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # cert_serial index — the CRL / revocation-check Wave-D-or-later
    # work will SELECT by serial. Single-row hit; cheap.
    op.create_index(
        "ix_appliance_cert_serial",
        "appliance",
        ["cert_serial"],
        unique=True,
        postgresql_where=sa.text("cert_serial IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_appliance_cert_serial", table_name="appliance")
    op.drop_column("appliance", "rejected_by_user_id")
    op.drop_column("appliance", "rejected_at")
    op.drop_column("appliance", "approved_by_user_id")
    op.drop_column("appliance", "approved_at")
    op.drop_column("appliance", "cert_expires_at")
    op.drop_column("appliance", "cert_issued_at")
    op.drop_column("appliance", "cert_serial")
    op.drop_column("appliance", "cert_pem")
    op.drop_column("appliance", "session_token_hash")
    op.drop_column("appliance", "capabilities")
    op.drop_table("appliance_ca")
    op.drop_column("pairing_code", "code_encrypted")
