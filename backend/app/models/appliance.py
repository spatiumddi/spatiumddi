"""SpatiumDDI OS appliance management models — Phase 4 (issue #134).

Two persistence surfaces live here:

* ``ApplianceCertificate`` (Phase 4b.1) — Web UI TLS cert with a
  Fernet-encrypted private key.
* ``PairingCode`` (#169) — short-lived, single-use 8-digit codes that
  swap for the real agent bootstrap key. See the model docstring.

The broader management surface (releases, container state, host
network config, maintenance mode) doesn't need DB persistence — those
endpoints read from / write to systemd, docker, nftables directly.
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

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
    #
    # NULLable since Phase 4b.3 — a CSR-pending row carries a stored
    # private key + the generated CSR but no cert yet. cert_pem stays
    # NULL until the operator pastes back the signed cert. Treat
    # ``cert_pem IS NULL`` as the canonical "CSR pending" sentinel.
    cert_pem: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Identity extracted from the cert at upload time (so listing
    # doesn't need to re-parse every PEM on every request). subject_cn
    # is the leaf's CN; sans_json is the full SubjectAlternativeName
    # list (DNS names + IP addresses). issuer_cn is the CN of the
    # immediate issuer in the chain.
    #
    # For CSR-pending rows (Phase 4b.3) subject_cn + sans come from the
    # operator's CSR form (they're known up-front); issuer_cn /
    # fingerprint / validity dates aren't known until the signed cert
    # comes back, so those three are nullable.
    subject_cn: Mapped[str] = mapped_column(String(255), nullable=False)
    sans_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    issuer_cn: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # SHA-256 fingerprint of the leaf DER, hex-encoded with colons
    # (matches `openssl x509 -fingerprint -sha256` output). NULL on
    # CSR-pending rows.
    fingerprint_sha256: Mapped[str | None] = mapped_column(String(95), nullable=True)

    # NotBefore / NotAfter from the leaf cert. Used by the UI to
    # render "expires in N days" badges and by a future renewal task
    # (Phase 4b.4) to schedule Let's Encrypt rotations. NULL on
    # CSR-pending rows.
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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


# Deployment kinds a pairing code may target. ``"both"`` provisions an
# agent appliance that runs BIND9 + Kea simultaneously (one box, both
# services) — the consume endpoint returns both bootstrap keys in that
# case. The fully generic ``"agent"`` role (post-join role assignment
# from the control plane) is #170.
PAIRING_KIND_DNS = "dns"
PAIRING_KIND_DHCP = "dhcp"
PAIRING_KIND_BOTH = "both"
PAIRING_KINDS = (PAIRING_KIND_DNS, PAIRING_KIND_DHCP, PAIRING_KIND_BOTH)


class PairingCode(Base):
    """Short-lived, single-use code that an agent installer swaps for
    the real ``DNS_AGENT_KEY`` / ``DHCP_AGENT_KEY`` bootstrap key.

    Operator generates the code on the control plane (8 decimal
    digits, default 15-min expiry, optional pre-assigned group). Agent
    POSTs ``/api/v1/appliance/pair {code, hostname}`` to redeem it.

    Security model:

    * Code is stored as sha256 — the cleartext is shown exactly once
      on creation and never persisted. An attacker with read access
      to this table can't trivially replay a pending code, though
      sha256 of 8-digit decimal IS rainbow-tableable; this is
      defense-in-depth, not the primary gate.
    * Single-use: ``used_at`` non-null disqualifies subsequent
      attempts.
    * Time-bound: ``expires_at`` enforced at consume time, plus a
      Celery reaper that DELETEs old rows.
    * Audit log captures create / claim / revoke / expire so abusive
      consume attempts are at least visible after the fact.
    """

    __tablename__ = "pairing_code"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # sha256 hex digest of the cleartext code. UNIQUE so the consume
    # endpoint can look up by hash in O(log n) without collision risk.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # Last two digits of the cleartext code. Surfaced in the list
    # endpoint for visual correlation ("which row is the code I just
    # wrote down?"). Two digits is trivial entropy — security comes
    # from the full 8 digits + expiry + single-use, not from this.
    code_last_two: Mapped[str] = mapped_column(String(2), nullable=False)

    # ``dns`` | ``dhcp`` — picks which bootstrap key the consume endpoint
    # returns. Enforced at the DB layer by a CHECK constraint (see
    # migration) so we can never silently issue a code for an unknown
    # kind even if the API layer bug-paths around its own validator.
    deployment_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    # Optional pre-assignment. Stored as a free-form UUID rather than
    # a FK because the column is polymorphic across
    # ``dns_server_group`` and ``dhcp_server_group``; the kind picks
    # which table the UUID points into. The create endpoint validates
    # the group actually exists for the given kind.
    server_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Wall-clock expiry. Codes past their expiry are refused at consume
    # time even before the reaper sweeps them.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Claim state. All three set atomically when the consume endpoint
    # succeeds; ``used_at`` non-null is the canonical "this code is
    # dead" signal.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    used_by_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Operator-driven cancellation. Independent of ``used_at`` —
    # revoking a code that's already been claimed is a no-op for the
    # consume endpoint but still useful audit signal.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    # Free-form operator note, e.g. "for dns-west-2". Surfaced in the
    # codes list + audit log.
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
