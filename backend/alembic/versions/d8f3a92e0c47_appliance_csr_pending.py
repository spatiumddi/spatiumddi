"""appliance_certificate — allow CSR-pending rows (Phase 4b.3)

A CSR-pending row is created when the operator hits "Generate CSR":
we generate a private key + CSR locally, store both, and wait for the
operator to paste back the signed cert. While in that pending state
the cert-derived columns (cert_pem / issuer_cn / fingerprint_sha256 /
valid_from / valid_to) are not yet known — they get populated when
the signed cert lands via /tls/{id}/import-cert. Relax those columns
to NULLable so the pending row can exist.

Phase 4b.1 reserved the csr_pem + csr_subject columns themselves
(both already nullable) so this migration only adjusts the NOT NULL
constraints on the cert-derived columns.

Revision ID: d8f3a92e0c47
Revises: c9f2a83b04d7
Create Date: 2026-05-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d8f3a92e0c47"
down_revision: str | None = "c9f2a83b04d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("appliance_certificate", "cert_pem", nullable=True)
    op.alter_column("appliance_certificate", "issuer_cn", nullable=True)
    op.alter_column("appliance_certificate", "fingerprint_sha256", nullable=True)
    op.alter_column("appliance_certificate", "valid_from", nullable=True)
    op.alter_column("appliance_certificate", "valid_to", nullable=True)


def downgrade() -> None:
    # Downgrading is only safe if no CSR-pending rows exist, otherwise
    # the NOT NULL constraint would fail. Delete pending rows first.
    op.execute("DELETE FROM appliance_certificate WHERE cert_pem IS NULL")
    op.alter_column(
        "appliance_certificate",
        "valid_to",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "appliance_certificate",
        "valid_from",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "appliance_certificate",
        "fingerprint_sha256",
        existing_type=sa.String(length=95),
        nullable=False,
    )
    op.alter_column(
        "appliance_certificate",
        "issuer_cn",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "appliance_certificate",
        "cert_pem",
        existing_type=sa.Text(),
        nullable=False,
    )
