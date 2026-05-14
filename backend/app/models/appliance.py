"""SpatiumDDI OS appliance management models — Phase 4 (issue #134).

Three persistence surfaces live here:

* ``ApplianceCertificate`` (Phase 4b.1) — Web UI TLS cert with a
  Fernet-encrypted private key.
* ``PairingCode`` (#169) — short-lived, single-use 8-digit codes that
  swap for the real agent bootstrap key. See the model docstring.
* ``Appliance`` (#170 Wave A2) — one row per supervisor that's claimed
  a pairing code. Carries the supervisor's Ed25519 public key +
  identity metadata. See the model docstring.

The broader management surface (releases, container state, host
network config, maintenance mode) doesn't need DB persistence — those
endpoints read from / write to systemd, docker, nftables directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, func
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


class PairingCode(Base):
    """Pairing code minted by an admin so a new supervisor appliance
    can join the fleet (#169 + #170 Wave A3 reshape).

    Two flavours:

    * **Ephemeral** (``persistent=False``, today's behaviour). Single-
      use, short expiry (default 15 min). Operator mints one per
      install. Consumed via ``POST /api/v1/appliance/supervisor/
      register`` — the consume side writes a ``pairing_claim`` row
      against the new ``appliance`` and the code is dead.
    * **Persistent** (``persistent=True``). Re-usable across N
      appliances (think "the staging-fleet code"). Default no expiry;
      admin can set one. ``enabled`` toggles whether new claims are
      accepted without revoking the code. ``max_claims`` optionally
      caps the number of claims; NULL = unlimited.

    Security model:

    * Code is stored as sha256 — the cleartext is shown exactly once
      on creation and persisted nowhere. Persistent codes can be
      *re-displayed* via a password-gated reveal endpoint that rotates
      the code (mint a new cleartext + replace code_hash atomically;
      existing claims are unaffected since FKs live on ``id``).
      Ephemeral codes are NOT re-displayable — losing the cleartext
      means minting a new ephemeral code.
    * ``revoked_at`` permanently kills a code. ``enabled=False`` on
      a persistent code temporarily pauses new claims without
      losing the row.
    * Claim accounting lives in the ``pairing_claim`` child table —
      one row per (code, supervisor) successful claim. The presence
      of any claim against an ephemeral code disqualifies it from
      future claims.
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

    # Wall-clock expiry. NULL = no expiry (persistent codes default
    # to this; admin can override). Ephemeral codes always carry an
    # expiry — validated at the API layer.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # When True the code can be claimed by N appliances; when False
    # it's single-use (today's #169 default). A claim sweep at the
    # supervisor-register endpoint enforces single-use by checking
    # for any existing pairing_claim row.
    persistent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Only meaningful for persistent=True. Admin can pause new claims
    # without deleting the code (e.g. "freeze the staging fleet code
    # until the migration finishes"). Already-claimed appliances are
    # unaffected — their cert lives on its own track.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Optional ceiling on claims for persistent codes. NULL = unlimited.
    # Lets an operator hand out a "this code admits up to 50 boxes"
    # token without re-issuing.
    max_claims: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Fernet-encrypted cleartext of the 8-digit code. Populated ONLY
    # for persistent codes — the /reveal endpoint decrypts it after a
    # password re-check. Ephemeral codes leave this NULL (cleartext is
    # shown once on create and gone forever, matching #169 semantics).
    code_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Operator-driven cancellation. Independent of enabled —
    # revoking a code is permanent ("dead row"), disabling is
    # reversible ("paused"). Revoking a claimed code is a no-op for
    # already-issued certs but still useful audit signal.
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


class PairingClaim(Base):
    """One row per (pairing_code, supervisor) successful claim.

    Ephemeral codes: at most one row (subsequent claim attempts hit
    the single-use gate). Persistent codes: many rows, one per
    registered supervisor. The UNIQUE(pairing_code_id, appliance_id)
    constraint makes the re-register-from-cache idempotent path
    (supervisor restarts mid-claim, retries with same pubkey) safe:
    the second call hits the existing row instead of writing a
    duplicate.

    ON DELETE CASCADE both ways — deleting a pairing code drops its
    claim audit; deleting an approved appliance drops its claim row.
    The permanent audit-log row carries the durable history.
    """

    __tablename__ = "pairing_claim"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pairing_code_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pairing_code.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    appliance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("appliance.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_from_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)


# Supervisor lifecycle states (#170 Wave A2). State machine:
#
#   pending_approval ──(admin Approve in B1)──▶ approved
#         │                                        │
#         └────────(admin Reject)─────────────┐    │
#                                             ▼    ▼
#                                          (row DELETEd, supervisor
#                                           re-bootstraps on next poll)
#
# ``rejected`` is reserved for an optional intermediate state in B1
# where admin "rejects but retains audit trail" — A2 never writes it.
APPLIANCE_STATE_PENDING_APPROVAL = "pending_approval"
APPLIANCE_STATE_APPROVED = "approved"
APPLIANCE_STATE_REJECTED = "rejected"
APPLIANCE_STATES = (
    APPLIANCE_STATE_PENDING_APPROVAL,
    APPLIANCE_STATE_APPROVED,
    APPLIANCE_STATE_REJECTED,
)


class Appliance(Base):
    """One row per supervisor that's claimed a pairing code (#170).

    The supervisor generates an Ed25519 keypair on first boot, posts
    its public key + a pairing code to
    ``POST /api/v1/appliance/supervisor/register``, and the control
    plane lands an ``Appliance`` row in ``pending_approval`` state.
    Wave B1 wires admin approval + cert signing on top.

    Identity model:

    * ``public_key_der`` is the supervisor's Ed25519 pubkey, DER-
      encoded. Stored verbatim so the B1 cert signer can re-derive
      identity material without re-parsing.
    * ``public_key_fingerprint`` is sha256(public_key_der) hex-encoded.
      UNIQUE — a supervisor that resubmits the same pubkey (typical
      restart-after-crash) hits the same row and the register endpoint
      replies "already registered" idempotently. A NEW pubkey from the
      same hostname creates a NEW row (admin sees two pending entries
      and approves the real one).

    Reject / delete semantics:

    * The state column carries ``pending_approval`` → ``approved`` /
      ``rejected``, but in practice admins drop pending or approved
      rows by DELETE — the supervisor sees its ``appliance_id`` 404
      on next poll and falls back into "waiting for pairing code"
      state. We keep the column for audit-trail-style states the B1
      / Wave-D fleet UI may want.

    No FK to ``pairing_code`` is enforced beyond ON DELETE SET NULL,
    so Wave A3's pairing-code reaper sweeping terminal codes doesn't
    take down the appliances those codes provisioned.
    """

    __tablename__ = "appliance"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    hostname: Mapped[str] = mapped_column(String(255), nullable=False)

    # Raw Ed25519 public key, DER-encoded.
    public_key_der: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # sha256(public_key_der) hex-encoded — 64 chars. Globally unique
    # across the fleet; a duplicate submission = re-register-from-cache
    # and short-circuits to "you already exist".
    public_key_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    # Free-form version string reported by the supervisor at register
    # time, e.g. "2026.05.14-1". Used by the fleet UI's needs-upgrade
    # banner.
    supervisor_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    paired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    paired_from_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Pointer at the pairing_code row that admitted this register. ON
    # DELETE SET NULL — Wave A3's pairing-code reaper sweeps old
    # terminal codes; we don't want those sweeps to take down the
    # appliances they provisioned.
    paired_via_code_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pairing_code.id", ondelete="SET NULL"),
        nullable=True,
    )

    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=APPLIANCE_STATE_PENDING_APPROVAL
    )

    # Updated by Wave A2+'s supervisor heartbeat path. Stays NULL
    # until the supervisor's first post-register check-in.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Wave B1 — supervisor-reported capabilities (can_run_dns_bind9,
    # has_baked_images, cpu_count, host_nics, …). Populated on
    # register + every heartbeat; the fleet UI's role picker filters
    # against this column. Free-form JSONB (no DB-side validation) so
    # additive supervisor versions don't need a migration each time
    # a new fact gets reported.
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # sha256 of the unauth session token the supervisor uses between
    # register and approval. The register response returns the
    # cleartext once; subsequent /supervisor/poll calls present it
    # for constant-time verification. Cleared after cert issuance —
    # all post-approval calls authenticate via mTLS.
    session_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Cert lifecycle (#170 B1). Populated by the approve endpoint:
    # CA signs an X.509 cert binding the supervisor's Ed25519 pubkey
    # to the appliance_id (subject CN). 90-day default validity; the
    # supervisor auto-renews 30 days before expiry (Wave C polish).
    cert_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    cert_serial: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cert_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cert_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Approval audit columns (the canonical state is still `state` —
    # these timestamps are for UI relative-time chips).
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    # Slot telemetry — #170 Wave C1 moves this off dns_server /
    # dhcp_server (the per-service agents used to report it
    # independently in #138 Phase 8f-2). The supervisor's heartbeat
    # is now the single producer; the fleet UI reads these columns
    # to drive the Upgrade affordance, slot chips, and reboot button.
    deployment_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    installed_appliance_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_slot: Mapped[str | None] = mapped_column(String(16), nullable=True)
    durable_default: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_trial_boot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    last_upgrade_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_upgrade_state_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    snmpd_running: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ntp_sync_state: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Operator-driven desired state. Set via the fleet UI / API;
    # supervisor's heartbeat poll picks them up + writes the matching
    # trigger files on the appliance host. Heartbeat handler auto-
    # clears once installed catches up (upgrade) or a fresh heartbeat
    # arrives post-reboot.
    desired_appliance_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_slot_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    reboot_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    reboot_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Role assignment — #170 Wave C2. Operator picks a subset of
    # ``dns-bind9`` / ``dns-powerdns`` / ``dhcp`` / ``observer`` /
    # ``custom``. Mutually-exclusive pairs (one DNS engine per box)
    # enforced at the role-assignment endpoint, not via a CHECK
    # constraint (operator intent should be a one-line API error,
    # not a Postgres exception).
    assigned_roles: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    assigned_dns_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="SET NULL"),
        nullable=True,
    )
    assigned_dhcp_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dhcp_server_group.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Operator-defined free-form key:value (string) pairs for fleet
    # targeting. ``{"site": "prod-east", "tier": "edge"}``. No
    # semantic interpretation; consumed by future fleet-UI filters +
    # MCP `tags_match` query arg.
    tags: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )

    # #170 Wave C3 — free-form nftables fragment the supervisor
    # renders **after** the role-driven block in
    # /etc/nftables.d/spatium-role.nft. Empty / NULL → role-driven
    # rules only. Operator typo-rejected via ``nft -c -f`` dry-run
    # on the supervisor before live-swap; rejection never opens or
    # closes the firewall mid-render.
    firewall_extra: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ApplianceCA(Base):
    """Internal CA singleton (#170 Wave B1).

    One row, id=1. Carries the RSA-2048 root cert + Fernet-encrypted
    private key that signs every supervisor's identity cert. Generated
    lazily on first need (first approve attempt) so a fresh-install
    control plane that never approves a supervisor doesn't pay the
    cost.

    Lifetime: 10 years by default. The CA's own rotation is a Wave-D
    polish — not in scope here. Operators wanting to migrate to a
    new CA today would re-key every approved supervisor manually.
    """

    __tablename__ = "appliance_ca"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    subject_cn: Mapped[str] = mapped_column(String(255), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    cert_pem: Mapped[str] = mapped_column(Text, nullable=False)
    key_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
