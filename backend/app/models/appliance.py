"""SpatiumDDI OS appliance management models — Phase 4 (issue #134).

Only the Web UI certificate is modeled here today (Phase 4b.1). The
broader management surface (releases, container state, host network
config, maintenance mode) doesn't need DB persistence — those endpoints
read from / write to systemd, docker, nftables directly. The certificate
is different because it carries a Fernet-encrypted private key, has
identity (subject/SAN/fingerprint) we want to display historically, and
needs an explicit "which one is active" pointer that survives reboot.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


# Certificate sources — also the operator-facing label on each
# certificate card. "uploaded" is Phase 4b.1; the other three land
# in 4b.3 / 4b.4 / 4b.5 respectively, but we put the column in now
# so we don't need a follow-up migration for an enum widening.
CERT_SOURCE_UPLOADED = "uploaded"  # operator pasted/uploaded PEM
CERT_SOURCE_CSR = "csr"  # generated locally, signed by external CA
CERT_SOURCE_LETSENCRYPT = "letsencrypt"  # issued via ACME
CERT_SOURCE_SELF_SIGNED = "self-signed"  # auto-generated on first boot


class ApplianceCertificate(Base):
    """TLS certificate for the appliance's HTTPS frontend.

    Multiple certificates can live in the table (old + new during a
    rotation, or a self-signed fallback alongside the real one). The
    ``is_active`` flag picks which one nginx serves. The activation
    endpoint enforces the invariant that at most one row carries
    ``is_active=True``; we don't use a partial unique index because
    the swap-old-for-new flow temporarily has neither/both flagged
    inside the same transaction.
    """

    __tablename__ = "appliance_certificate"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Operator-chosen label. Unique so audit log lines + UI cards can
    # refer to "letsencrypt-2026.05" without ambiguity. Distinct from
    # the certificate's subject CN — the cert might be `*.spatiumddi.io`
    # but the operator names the row "wildcard-prod" for their own
    # bookkeeping.
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)

    # How this row arrived (see the CERT_SOURCE_* constants above).
    # Stored as a plain string rather than an Enum so adding sources
    # later doesn't require a migration to widen the type.
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    # PEM-encoded certificate chain. Operators paste the full chain
    # (leaf + intermediates); we don't split because nginx wants the
    # concatenated file anyway. Plain text — public material.
    cert_pem: Mapped[str] = mapped_column(Text, nullable=False)

    # PEM-encoded private key, Fernet-encrypted at rest. NEVER returned
    # in any API response — only written to /etc/nginx/certs/active.key
    # on the appliance when this row becomes active. Stored separately
    # from cert_pem so we can encrypt without affecting how the cert
    # body is rendered in the UI.
    key_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Pointer to "the one nginx serves". At most one row may have
    # is_active=True; the /tls/{id}/activate endpoint clears every
    # other row's flag before setting this one. activated_at remembers
    # the most recent activation timestamp so the UI can show "active
    # since X" without consulting the audit log.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Identity extracted from the cert at upload time (so listing
    # doesn't need to re-parse every PEM on every request). subject_cn
    # is the leaf's CN; sans_json is the full SubjectAlternativeName
    # list (DNS names + IP addresses). issuer_cn is the CN of the
    # immediate issuer in the chain.
    subject_cn: Mapped[str] = mapped_column(String(255), nullable=False)
    sans_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    issuer_cn: Mapped[str] = mapped_column(String(255), nullable=False)

    # SHA-256 fingerprint of the leaf DER, hex-encoded with colons
    # (matches `openssl x509 -fingerprint -sha256` output).
    fingerprint_sha256: Mapped[str] = mapped_column(String(95), nullable=False)

    # NotBefore / NotAfter from the leaf cert. Used by the UI to
    # render "expires in N days" badges and by a future renewal task
    # (Phase 4b.4) to schedule Let's Encrypt rotations.
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Operator notes — purely descriptive. UI shows them on the card
    # for context ("Let's Encrypt prod cert — renew script in cron").
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Audit metadata.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    # CSR-pending state (Phase 4b.3) — when the operator clicks
    # "Generate CSR" we create a row with cert_pem and these CSR fields
    # populated, no fingerprint yet, is_active false. Once they paste
    # back the signed cert we move it into cert_pem and null the CSR
    # fields. Wired into the model now so 4b.3 doesn't need a second
    # migration; ignored by 4b.1's upload flow.
    csr_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    csr_subject: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
