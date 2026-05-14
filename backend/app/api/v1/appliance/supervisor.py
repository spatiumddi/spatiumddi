"""Supervisor register endpoint (#170 Wave A2).

Lands the new ``POST /api/v1/appliance/supervisor/register`` surface
the new ``spatium-supervisor`` container will call on first boot.
Behind ``platform_settings.supervisor_registration_enabled`` — default
FALSE, so Wave A's landing doesn't change behaviour for any existing
dns / dhcp agent install (which still uses ``/dns/agents/register`` /
``/dhcp/agents/register`` + the long PSK).

Flow:

1. Supervisor generates an Ed25519 keypair on first boot, persists
   the private key on ``/var/persist/spatium-supervisor/``.
2. Supervisor POSTs ``{pairing_code, hostname, public_key_der_b64,
   supervisor_version}`` here.
3. Endpoint validates the pairing code via the existing #169 single-
   use code logic (sha256 lookup, expiry / revoked / already-used
   checks, constant-time friction sleep on every failure mode).
4. On success, atomically marks the code claimed + writes an
   ``appliance`` row in ``pending_approval`` state + emits two audit
   rows (one for the pairing-code claim, one for the registration).
5. Returns ``{appliance_id, state}`` — no bootstrap key, no JWT, no
   cert. Cert signing lands in Wave B1; until then a registered
   supervisor sits in pending until an admin clicks Approve.

Re-register-from-cache semantics: if the supervisor restarts before
hearing back (network blip, container restart), it submits the SAME
public key. We detect the duplicate by ``public_key_fingerprint`` and
short-circuit to "you already exist, here's your appliance_id" rather
than refusing the call — this lets the supervisor recover without
needing a fresh pairing code. The original pairing code stays claimed
to its original IP / hostname.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from typing import Literal

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
)
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func as sa_func
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.core.permissions import require_permission
from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    APPLIANCE_STATE_PENDING_APPROVAL,
    Appliance,
    PairingClaim,
    PairingCode,
)
from app.models.audit import AuditLog
from app.models.settings import PlatformSettings
from app.services.appliance.ca import (
    ensure_ca,
    generate_session_token,
    sign_supervisor_cert,
    verify_session_token,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


# Pairing-code constants — kept private to this module rather than
# imported from ``pairing.py`` so the supervisor flow doesn't pick up
# unrelated changes if the existing surface is reshaped in A3.
_CODE_LENGTH = 8
_CONSUME_FAILURE_DELAY_S = 0.5


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ── Schemas ────────────────────────────────────────────────────────


class SupervisorCapabilities(BaseModel):
    """Facts the supervisor advertises on register + every heartbeat.

    All fields optional / defaulted — additive evolution is the only
    forward-compat shape we'll need; the control plane stores the
    block verbatim and ignores keys it doesn't recognise. New
    capability flags ship in supervisor releases and surface on the
    control plane without a migration.
    """

    can_run_dns_bind9: bool = False
    can_run_dns_powerdns: bool = False
    can_run_dhcp: bool = False
    can_run_observer: bool = False
    has_baked_images: bool = False
    baked_images_version: str | None = None
    cpu_count: int | None = None
    memory_mb: int | None = None
    storage_type: str | None = None
    host_nics: list[str] = Field(default_factory=list)


class SupervisorRegisterRequest(BaseModel):
    pairing_code: str = Field(
        min_length=_CODE_LENGTH,
        max_length=_CODE_LENGTH,
        description="8-digit pairing code minted by an admin in the Pairing tab.",
    )
    hostname: str = Field(
        min_length=1,
        max_length=255,
        description="Operator-supplied hostname; surfaces in the fleet UI.",
    )
    public_key_der_b64: str = Field(
        description=(
            "Base64-encoded DER form of the supervisor's Ed25519 "
            "public key. The supervisor generates the keypair on "
            "first boot and stores the private half on /var so it "
            "survives slot swaps."
        )
    )
    supervisor_version: str | None = Field(
        default=None,
        max_length=64,
        description="Supervisor build version, e.g. '2026.05.14-1'.",
    )
    capabilities: SupervisorCapabilities | None = Field(
        default=None,
        description=(
            "Supervisor-advertised facts used by the control plane "
            "to filter role assignment options."
        ),
    )

    @field_validator("pairing_code")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("Pairing code must be 8 decimal digits.")
        return v


class SupervisorRegisterResponse(BaseModel):
    """Register response shape. The supervisor stashes ``session_token``
    locally and uses it for ``/supervisor/poll`` until the appliance
    is approved; after approval it switches to mTLS with the cert
    that ``/supervisor/poll`` returns.
    """

    appliance_id: uuid.UUID
    state: Literal["pending_approval", "approved"]
    # Echoed back for the supervisor's audit log line, so the log
    # reads "registered as ab-cd-… with fingerprint 1234abcd…" without
    # needing to re-derive the hash on the client side.
    public_key_fingerprint: str
    # One-time token returned to the supervisor for /supervisor/poll
    # calls until cert issuance. Stored sha256'd on the appliance row;
    # cleartext is shown exactly once. On re-register-from-cache
    # (idempotent path) the token is rotated — the previous one is
    # implicitly invalidated by hash mismatch.
    session_token: str


class SupervisorPollRequest(BaseModel):
    appliance_id: uuid.UUID
    session_token: str = Field(min_length=1)


class SupervisorPollResponse(BaseModel):
    """Polled by the supervisor every few seconds between register
    and approval. Once approved, ``cert_pem`` + ``ca_chain_pem`` are
    populated and the supervisor switches to mTLS for everything else.
    """

    appliance_id: uuid.UUID
    state: Literal["pending_approval", "approved"]
    cert_pem: str | None = None
    ca_chain_pem: str | None = None
    cert_expires_at: datetime | None = None


# ── Helpers ────────────────────────────────────────────────────────


def _decode_pubkey(payload: str) -> tuple[bytes, str]:
    """Return (der_bytes, sha256_fingerprint_hex). Raises HTTPException
    422 on any decoding / parsing failure."""
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "public_key_der_b64 is not valid base64.",
        ) from exc
    if len(raw) > 1024:
        # Ed25519 DER is 44 bytes; anything over 1 KB is malformed or
        # an attempted resource exhaustion via a giant blob.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "public_key_der_b64 exceeds 1024 bytes.",
        )
    try:
        pubkey = load_der_public_key(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "public_key_der_b64 is not a parseable DER public key.",
        ) from exc
    if not isinstance(pubkey, Ed25519PublicKey):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "public_key_der_b64 must be an Ed25519 public key.",
        )
    # Re-serialise to canonical SubjectPublicKeyInfo DER so the
    # fingerprint is over a stable form regardless of which encoding
    # the caller submitted (e.g. raw 32-byte key wrapped vs already-
    # SPKI). Cryptography rejected the raw form above; here we just
    # normalise.
    canonical = pubkey.public_bytes(
        encoding=Encoding.DER,
        format=PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = hashlib.sha256(canonical).hexdigest()
    return canonical, fingerprint


async def _module_enabled(db: DB) -> bool:
    """Read the feature flag. Cached settings row is fine — operator
    flipping the toggle takes effect on the next API call without a
    process restart."""
    stmt = select(PlatformSettings).where(PlatformSettings.id == 1)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return False
    return bool(row.supervisor_registration_enabled)


# ── Endpoint ───────────────────────────────────────────────────────


@router.post(
    "/supervisor/register",
    response_model=SupervisorRegisterResponse,
    summary="Register a spatium-supervisor with a pairing code (unauthenticated)",
)
async def supervisor_register(
    body: SupervisorRegisterRequest,
    request: Request,
    db: DB,
) -> SupervisorRegisterResponse:
    """Register a supervisor + claim its pairing code in one shot.

    UNAUTHENTICATED — the pairing code IS the auth, same shape as
    ``POST /api/v1/appliance/pair``. Failure modes are deliberately
    collapsed into a single 403 with a constant friction delay so a
    brute-forcer can't distinguish "wrong code" from "expired" from
    "supervisor registration disabled" via timing or response shape.
    """
    # Feature gate. While disabled the endpoint behaves exactly like
    # "not found" — same shape an attacker probing for the path would
    # see if it didn't exist. Sleep before the 404 so toggling the
    # flag doesn't reveal itself via response-time delta.
    if not await _module_enabled(db):
        await asyncio.sleep(_CONSUME_FAILURE_DELAY_S)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Not found.",
        )

    # Parse + normalise pubkey before touching the pairing-code path —
    # 422s on a malformed pubkey shouldn't burn a real pairing code.
    pubkey_der, pubkey_fingerprint = _decode_pubkey(body.public_key_der_b64)

    submitted_hash = _hash_code(body.pairing_code)
    client_ip = _client_ip(request)
    now = datetime.now(UTC)

    # Re-register-from-cache short circuit — same pubkey resubmits hit
    # the same row. We don't even validate the pairing code in this
    # case (the original registration already burned it, and re-
    # submitting the same code would 403 here, locking the supervisor
    # out of recovery). The supervisor's local state-dir is the
    # source of truth for its identity; if the operator wants to
    # force a fresh registration they delete the row in the fleet UI,
    # which causes the supervisor to clear its identity + claim a new
    # pairing code on next boot.
    existing_stmt = select(Appliance).where(Appliance.public_key_fingerprint == pubkey_fingerprint)
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        # Idempotent re-register. Touch last_seen_at so the heartbeat
        # path stays useful even before A2+ ships a separate
        # heartbeat endpoint. Update capabilities + supervisor_version
        # since the supervisor may have upgraded between calls.
        existing.last_seen_at = now
        existing.last_seen_ip = client_ip
        if body.supervisor_version:
            existing.supervisor_version = body.supervisor_version
        if body.capabilities is not None:
            existing.capabilities = body.capabilities.model_dump()
        # Rotate the session token — supervisor must use the fresh one
        # for subsequent polls. Old cached tokens fail hash compare.
        # Skip when the row already has a cert (approval-complete; the
        # supervisor is using mTLS not session tokens at that point).
        if existing.cert_pem is None:
            cleartext, digest = generate_session_token()
            existing.session_token_hash = digest
        else:
            cleartext = ""  # no longer needed; supervisor uses mTLS
        await db.commit()
        logger.info(
            "supervisor_register_idempotent",
            appliance_id=str(existing.id),
            fingerprint=pubkey_fingerprint,
            ip=client_ip,
        )
        return SupervisorRegisterResponse(
            appliance_id=existing.id,
            state=existing.state,  # type: ignore[arg-type]
            public_key_fingerprint=pubkey_fingerprint,
            session_token=cleartext,
        )

    # Look up pairing code by hash. Single-row index hit.
    stmt = select(PairingCode).where(PairingCode.code_hash == submitted_hash)
    code_row = (await db.execute(stmt)).scalar_one_or_none()

    # Claim count for the code — disambiguates ephemeral (single-use)
    # from persistent (multi-use) gating below.
    claim_count = 0
    if code_row is not None:
        claim_count = int(
            (
                await db.execute(
                    select(sa_func.count(PairingClaim.id)).where(
                        PairingClaim.pairing_code_id == code_row.id
                    )
                )
            ).scalar_one()
        )

    failure_reason: str | None = None
    if code_row is None:
        failure_reason = "unknown_code"
    elif code_row.revoked_at is not None:
        failure_reason = "revoked"
    elif code_row.expires_at is not None and code_row.expires_at <= now:
        failure_reason = "expired"
    elif not code_row.persistent and claim_count > 0:
        # Ephemeral codes: any prior claim disqualifies the code from
        # future claims (today's #169 single-use semantics).
        failure_reason = "already_used"
    elif code_row.persistent and not code_row.enabled:
        # Persistent code paused by admin.
        failure_reason = "disabled"
    elif (
        code_row.persistent
        and code_row.max_claims is not None
        and claim_count >= code_row.max_claims
    ):
        failure_reason = "exhausted"

    if failure_reason is not None:
        db.add(
            AuditLog(
                user_id=None,
                user_display_name="anonymous supervisor",
                auth_source="anonymous",
                source_ip=client_ip,
                action="appliance.supervisor_register_denied",
                resource_type="pairing_code",
                resource_id=str(code_row.id) if code_row is not None else "unknown",
                resource_display=(
                    "supervisor pairing code" if code_row is not None else "unknown pairing code"
                ),
                result="forbidden",
                new_value={
                    "reason": failure_reason,
                    "hostname": body.hostname,
                    "fingerprint": pubkey_fingerprint,
                },
            )
        )
        await db.commit()
        logger.warning(
            "supervisor_register_denied",
            reason=failure_reason,
            ip=client_ip,
            hostname=body.hostname,
            fingerprint=pubkey_fingerprint,
        )
        await asyncio.sleep(_CONSUME_FAILURE_DELAY_S)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Pairing code is invalid, expired, or already used. "
            "Ask your platform admin to generate a new code.",
        )

    assert code_row is not None
    # Constant-time hash compare — index lookup already matched on
    # exact equality, but the canonical idiom keeps future refactors
    # honest.
    if not hmac.compare_digest(code_row.code_hash, submitted_hash):  # pragma: no cover
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Hash mismatch.")

    # Create the appliance row + the pairing_claim audit row
    # atomically. PairingCode no longer carries used_at semantics —
    # claim accounting lives in pairing_claim now (A3).
    appliance_id = uuid.uuid4()
    session_cleartext, session_hash = generate_session_token()
    appliance_row = Appliance(
        id=appliance_id,
        hostname=body.hostname,
        public_key_der=pubkey_der,
        public_key_fingerprint=pubkey_fingerprint,
        supervisor_version=body.supervisor_version,
        capabilities=(body.capabilities.model_dump() if body.capabilities else {}),
        session_token_hash=session_hash,
        paired_at=now,
        paired_from_ip=client_ip,
        paired_via_code_id=code_row.id,
        state=APPLIANCE_STATE_PENDING_APPROVAL,
    )
    db.add(appliance_row)
    db.add(
        PairingClaim(
            pairing_code_id=code_row.id,
            appliance_id=appliance_id,
            claimed_at=now,
            claimed_from_ip=client_ip,
            hostname=body.hostname,
        )
    )

    # Audit row pair — claim + registration. Two rows make it easier
    # to filter for "appliance lifecycle" in the audit UI without
    # losing pairing-code accountability.
    db.add(
        AuditLog(
            user_id=None,
            user_display_name=body.hostname,
            auth_source="pairing_code",
            source_ip=client_ip,
            action="appliance.pairing_code_claimed",
            resource_type="pairing_code",
            resource_id=str(code_row.id),
            resource_display="supervisor pairing code",
            result="success",
            new_value={
                "hostname": body.hostname,
                "appliance_id": str(appliance_id),
                "claim_kind": "supervisor",
            },
        )
    )
    db.add(
        AuditLog(
            user_id=None,
            user_display_name=body.hostname,
            auth_source="pairing_code",
            source_ip=client_ip,
            action="appliance.registration_pending",
            resource_type="appliance",
            resource_id=str(appliance_id),
            resource_display=body.hostname,
            result="success",
            new_value={
                "hostname": body.hostname,
                "fingerprint": pubkey_fingerprint,
                "supervisor_version": body.supervisor_version,
                "paired_via_code_id": str(code_row.id),
            },
        )
    )
    await db.commit()
    logger.info(
        "supervisor_registration_pending",
        appliance_id=str(appliance_id),
        hostname=body.hostname,
        fingerprint=pubkey_fingerprint,
        ip=client_ip,
    )

    return SupervisorRegisterResponse(
        appliance_id=appliance_id,
        state=APPLIANCE_STATE_PENDING_APPROVAL,
        public_key_fingerprint=pubkey_fingerprint,
        session_token=session_cleartext,
    )


# ── /supervisor/poll ───────────────────────────────────────────────


@router.post(
    "/supervisor/poll",
    response_model=SupervisorPollResponse,
    summary="Poll for approval state + cert (unauthenticated, session-token gated)",
)
async def supervisor_poll(
    body: SupervisorPollRequest,
    request: Request,
    db: DB,
) -> SupervisorPollResponse:
    """Polled by the supervisor every few seconds between register
    and approval. Verifies the appliance's session token (constant-
    time hash compare) and returns the current state. On approval
    the cert + CA chain are populated; the supervisor switches to
    mTLS for everything subsequent.

    Unauth — the session_token IS the auth. After approval the
    session_token is cleared from the row and this endpoint returns
    403; the supervisor is expected to be using mTLS by then.

    Like the other unauth endpoints, friction-sleep on every failure
    mode so an attacker probing for valid appliance_ids can't
    distinguish "wrong token" from "doesn't exist" via timing.
    """
    if not await _module_enabled(db):
        await asyncio.sleep(_CONSUME_FAILURE_DELAY_S)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    row = await db.get(Appliance, body.appliance_id)
    valid = row is not None and verify_session_token(body.session_token, row.session_token_hash)
    if not valid:
        await asyncio.sleep(_CONSUME_FAILURE_DELAY_S)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid appliance or session.")

    assert row is not None  # narrowed by valid above

    # Touch last-seen on every poll so the fleet UI knows the
    # supervisor is alive even before mTLS / heartbeat lands.
    row.last_seen_at = datetime.now(UTC)
    row.last_seen_ip = _client_ip(request)

    ca_chain_pem: str | None = None
    if row.cert_pem is not None:
        # Lazy CA fetch — the CA must exist if cert_pem is populated
        # (the approve endpoint guarantees this), but we still go
        # through ensure_ca() so a fresh DB without an approved
        # appliance yet doesn't fail this read.
        ca = await ensure_ca(db)
        ca_chain_pem = ca.cert_pem
        await db.commit()
    else:
        await db.commit()

    return SupervisorPollResponse(
        appliance_id=row.id,
        state=row.state,  # type: ignore[arg-type]
        cert_pem=row.cert_pem,
        ca_chain_pem=ca_chain_pem,
        cert_expires_at=row.cert_expires_at,
    )


# ── /supervisor/heartbeat ─────────────────────────────────────────


class SupervisorHeartbeatRequest(BaseModel):
    """Periodic heartbeat from the supervisor (#170 Wave C1).

    Carries the appliance-host telemetry block the service agents used
    to ship on their own per-row heartbeats in #138 Phase 8f-2. The
    supervisor is now the single source of truth for appliance-host
    state — service agents drop their host bind mounts in C1.

    Auth model is interim: session-token gated for now (same shape as
    /supervisor/poll). Wave C2/D will require mTLS once the verifier
    middleware lands, and the session_token field becomes optional
    (mTLS subject CN = appliance_id is the auth principal). Both
    paths persist the same fields.
    """

    appliance_id: uuid.UUID
    session_token: str | None = None
    capabilities: SupervisorCapabilities | None = None
    deployment_kind: Literal["appliance", "docker", "k8s", "unknown"] | None = None
    installed_appliance_version: str | None = None
    current_slot: Literal["slot_a", "slot_b"] | None = None
    durable_default: Literal["slot_a", "slot_b"] | None = None
    is_trial_boot: bool | None = None
    last_upgrade_state: Literal["ready", "in-flight", "done", "failed"] | None = None
    last_upgrade_state_at: datetime | None = None
    snmpd_running: bool | None = None
    ntp_sync_state: Literal["synchronized", "unsynchronized", "unknown"] | None = None


class SupervisorRoleAssignment(BaseModel):
    """Per-role config block the supervisor needs to bring up a
    service container (#170 Wave C2).

    DNS / DHCP groups carry an ``agent_key`` so the service container
    can register against the existing ``/dns/agents/register`` /
    ``/dhcp/agents/register`` endpoints. The key is the group-level
    bootstrap key the operator already revealed for that group; the
    supervisor passes it into the service container's env without
    operator action.
    """

    roles: list[str] = Field(default_factory=list)
    dns_group_id: uuid.UUID | None = None
    dns_group_name: str | None = None
    dns_engine: str | None = None  # bind9 / powerdns
    dhcp_group_id: uuid.UUID | None = None
    dhcp_group_name: str | None = None
    dhcp_network_mode: str | None = None  # host / bridged
    # #170 Wave C3 — operator-pasted nft fragment rendered after the
    # role-driven mgmt + per-role blocks. NULL / empty → role-driven
    # rules only.
    firewall_extra: str | None = None


class SupervisorHeartbeatResponse(BaseModel):
    """Operator-driven desired state returned to the supervisor.

    The supervisor compares each field to its local state + fires the
    matching trigger file on the appliance host. Idempotent — same
    desired_version returning across heartbeats produces one trigger
    write, not many (the trigger file's presence is the marker).
    """

    appliance_id: uuid.UUID
    state: Literal["pending_approval", "approved", "rejected"]
    desired_appliance_version: str | None = None
    desired_slot_image_url: str | None = None
    reboot_requested: bool = False
    # #170 Wave C2 — assigned roles + group config. The supervisor
    # uses this to bring up dns-bind9 / dns-powerdns / dhcp-kea
    # service containers via docker compose. Empty roles list = idle
    # (approved but no service running).
    role_assignment: SupervisorRoleAssignment = Field(default_factory=SupervisorRoleAssignment)


@router.post(
    "/supervisor/heartbeat",
    response_model=SupervisorHeartbeatResponse,
    summary="Supervisor heartbeat — appliance-host telemetry + desired state",
)
async def supervisor_heartbeat(
    body: SupervisorHeartbeatRequest,
    request: Request,
    db: DB,
) -> SupervisorHeartbeatResponse:
    """Persist supervisor-reported appliance-host state + return the
    operator's desired state for the supervisor to act on.

    Auth: session-token interim (per the SupervisorHeartbeatRequest
    docstring); after Wave C2/D this endpoint will be mTLS-gated and
    session_token becomes optional.

    On success:
    * Updates last_seen_at / last_seen_ip.
    * Overwrites the slot-telemetry columns when the supervisor
      reports non-None values (None leaves the column alone so a
      partial heartbeat doesn't blank fields the supervisor isn't
      currently sourcing).
    * Auto-clears ``desired_appliance_version`` once
      ``installed_appliance_version`` matches it AND the upgrade
      state is ``done`` or ``ready`` — the upgrade has landed and
      the operator's target is no longer load-bearing.
    * Auto-clears ``reboot_requested`` 15 s after the stamp on the
      assumption that the supervisor's heartbeat proves it survived
      the reboot. Same auto-heal shape as the legacy
      ``dns_server.reboot_requested`` / ``dhcp_server.reboot_requested``
      flags in Phase 8f-8.
    """
    if not await _module_enabled(db):
        await asyncio.sleep(_CONSUME_FAILURE_DELAY_S)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    row = await db.get(Appliance, body.appliance_id)
    valid = row is not None and (
        # Approved appliances no longer carry a session_token (B1
        # clears it on approve). Until mTLS lands they auth by the
        # same token re-presented from the supervisor's local cache
        # — but we'll accept any heartbeat from an approved row in
        # this interim window since the alternative is "no heartbeat
        # path until C2". B2's role lock-down narrows this back.
        row.state == APPLIANCE_STATE_APPROVED
        or (
            body.session_token is not None
            and verify_session_token(body.session_token, row.session_token_hash)
        )
    )
    if not valid:
        await asyncio.sleep(_CONSUME_FAILURE_DELAY_S)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid appliance or session.")

    assert row is not None

    row.last_seen_at = datetime.now(UTC)
    row.last_seen_ip = _client_ip(request)
    if body.capabilities is not None:
        row.capabilities = body.capabilities.model_dump()

    # Slot telemetry — only overwrite when the supervisor sent a non-
    # None value. Lets the supervisor send partial heartbeats (e.g.
    # NTP sidecar unreadable this tick) without blanking columns it
    # doesn't currently know about.
    if body.deployment_kind is not None:
        row.deployment_kind = body.deployment_kind
    if body.installed_appliance_version is not None:
        row.installed_appliance_version = body.installed_appliance_version
    if body.current_slot is not None:
        row.current_slot = body.current_slot
    if body.durable_default is not None:
        row.durable_default = body.durable_default
    if body.is_trial_boot is not None:
        row.is_trial_boot = body.is_trial_boot
    if body.last_upgrade_state is not None:
        row.last_upgrade_state = body.last_upgrade_state
    if body.last_upgrade_state_at is not None:
        row.last_upgrade_state_at = body.last_upgrade_state_at
    if body.snmpd_running is not None:
        row.snmpd_running = body.snmpd_running
    if body.ntp_sync_state is not None:
        row.ntp_sync_state = body.ntp_sync_state

    # Auto-clear desired_appliance_version once installed matches +
    # the upgrade landed cleanly. Same shape as #138 Phase 8f-4's
    # legacy dns_server / dhcp_server auto-clear.
    if (
        row.desired_appliance_version is not None
        and row.installed_appliance_version == row.desired_appliance_version
        and row.last_upgrade_state in (None, "done", "ready")
    ):
        row.desired_appliance_version = None
        row.desired_slot_image_url = None

    # Auto-clear reboot_requested 15 s after the stamp — by that
    # point the heartbeat is itself proof the reboot landed (or that
    # the reboot trigger has been written + the host runner is about
    # to fire). Reduces the chance of a stale flag stalling the
    # operator's next reboot request.
    if (
        row.reboot_requested
        and row.reboot_requested_at is not None
        and (datetime.now(UTC) - row.reboot_requested_at).total_seconds() >= 15
    ):
        row.reboot_requested = False
        row.reboot_requested_at = None

    await db.commit()

    # Resolve assigned role config so the supervisor can bring up
    # service containers. DNS / DHCP group lookups are best-effort —
    # if the operator deleted a group out from under an approved
    # appliance, the supervisor sees the role with a null group_id
    # and skips bringing the service container up (the empty role
    # set is itself the idle marker).
    role_assignment = await _build_role_assignment(db, row)

    return SupervisorHeartbeatResponse(
        appliance_id=row.id,
        state=row.state,  # type: ignore[arg-type]
        desired_appliance_version=row.desired_appliance_version,
        desired_slot_image_url=row.desired_slot_image_url,
        reboot_requested=row.reboot_requested,
        role_assignment=role_assignment,
    )


async def _build_role_assignment(db: DB, row: Appliance) -> SupervisorRoleAssignment:
    """Resolve the assigned roles + group identities for a supervisor
    heartbeat response. Best-effort — a missing group falls through
    as a null group_id; the supervisor treats that as "skip this
    role" (idle on the affected service)."""
    from app.models.dhcp import DHCPServerGroup
    from app.models.dns import DNSServerGroup

    dns_engine: str | None = None
    if "dns-bind9" in row.assigned_roles:
        dns_engine = "bind9"
    elif "dns-powerdns" in row.assigned_roles:
        dns_engine = "powerdns"

    dns_group_name: str | None = None
    if row.assigned_dns_group_id is not None:
        dns_group = await db.get(DNSServerGroup, row.assigned_dns_group_id)
        if dns_group is not None:
            dns_group_name = dns_group.name

    dhcp_group_name: str | None = None
    dhcp_network_mode: str | None = None
    if row.assigned_dhcp_group_id is not None:
        dhcp_group = await db.get(DHCPServerGroup, row.assigned_dhcp_group_id)
        if dhcp_group is not None:
            dhcp_group_name = dhcp_group.name
            dhcp_network_mode = dhcp_group.network_mode

    return SupervisorRoleAssignment(
        roles=list(row.assigned_roles or []),
        dns_group_id=row.assigned_dns_group_id,
        dns_group_name=dns_group_name,
        dns_engine=dns_engine,
        dhcp_group_id=row.assigned_dhcp_group_id,
        dhcp_group_name=dhcp_group_name,
        dhcp_network_mode=dhcp_network_mode,
        firewall_extra=row.firewall_extra,
    )


# ── Admin: approve / reject / delete / re-key ─────────────────────


class ApplianceRow(BaseModel):
    """Operator-facing summary of an appliance row. Cert / pubkey
    bytes are not exposed — only their derived metadata."""

    id: uuid.UUID
    hostname: str
    state: Literal["pending_approval", "approved", "rejected"]
    public_key_fingerprint: str
    supervisor_version: str | None
    capabilities: dict
    paired_at: datetime
    paired_from_ip: str | None
    last_seen_at: datetime | None
    last_seen_ip: str | None
    approved_at: datetime | None
    approved_by_user_id: uuid.UUID | None
    rejected_at: datetime | None
    cert_serial: str | None
    cert_issued_at: datetime | None
    cert_expires_at: datetime | None
    # #170 Wave C1 — slot telemetry surfaced from the appliance row.
    deployment_kind: str | None
    installed_appliance_version: str | None
    current_slot: str | None
    durable_default: str | None
    is_trial_boot: bool
    last_upgrade_state: str | None
    last_upgrade_state_at: datetime | None
    snmpd_running: bool | None
    ntp_sync_state: str | None
    desired_appliance_version: str | None
    desired_slot_image_url: str | None
    reboot_requested: bool
    reboot_requested_at: datetime | None
    # #170 Wave C2 — role assignment + free-form tags.
    assigned_roles: list[str]
    assigned_dns_group_id: uuid.UUID | None
    assigned_dhcp_group_id: uuid.UUID | None
    tags: dict[str, str]
    # #170 Wave C3 — operator-pasted nft fragment.
    firewall_extra: str | None
    created_at: datetime


class ApplianceList(BaseModel):
    appliances: list[ApplianceRow]


def _require_superadmin(user: CurrentUser) -> None:
    if not user.is_superadmin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Appliance approval is restricted to superadmins.",
        )


def _row_to_schema(row: Appliance) -> ApplianceRow:
    return ApplianceRow(
        id=row.id,
        hostname=row.hostname,
        state=row.state,  # type: ignore[arg-type]
        public_key_fingerprint=row.public_key_fingerprint,
        supervisor_version=row.supervisor_version,
        capabilities=row.capabilities or {},
        paired_at=row.paired_at,
        paired_from_ip=row.paired_from_ip,
        last_seen_at=row.last_seen_at,
        last_seen_ip=row.last_seen_ip,
        approved_at=row.approved_at,
        approved_by_user_id=row.approved_by_user_id,
        rejected_at=row.rejected_at,
        cert_serial=row.cert_serial,
        cert_issued_at=row.cert_issued_at,
        cert_expires_at=row.cert_expires_at,
        deployment_kind=row.deployment_kind,
        installed_appliance_version=row.installed_appliance_version,
        current_slot=row.current_slot,
        durable_default=row.durable_default,
        is_trial_boot=row.is_trial_boot,
        last_upgrade_state=row.last_upgrade_state,
        last_upgrade_state_at=row.last_upgrade_state_at,
        snmpd_running=row.snmpd_running,
        ntp_sync_state=row.ntp_sync_state,
        desired_appliance_version=row.desired_appliance_version,
        desired_slot_image_url=row.desired_slot_image_url,
        reboot_requested=row.reboot_requested,
        reboot_requested_at=row.reboot_requested_at,
        assigned_roles=list(row.assigned_roles or []),
        assigned_dns_group_id=row.assigned_dns_group_id,
        assigned_dhcp_group_id=row.assigned_dhcp_group_id,
        tags=dict(row.tags or {}),
        firewall_extra=row.firewall_extra,
        created_at=row.created_at,
    )


@router.get(
    "/appliances",
    response_model=ApplianceList,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="List registered appliances (superadmin)",
)
async def list_appliances(current_user: CurrentUser, db: DB) -> ApplianceList:
    _require_superadmin(current_user)
    rows = (
        (await db.execute(select(Appliance).order_by(Appliance.paired_at.desc()))).scalars().all()
    )
    return ApplianceList(appliances=[_row_to_schema(r) for r in rows])


@router.get(
    "/appliances/{appliance_id}",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Fetch a single appliance (superadmin)",
)
async def get_appliance(appliance_id: uuid.UUID, current_user: CurrentUser, db: DB) -> ApplianceRow:
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    return _row_to_schema(row)


@router.post(
    "/appliances/{appliance_id}/approve",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Approve a pending appliance — issues a cert (superadmin)",
)
async def approve_appliance(
    appliance_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> ApplianceRow:
    """Approve + sign in one shot. Idempotent on already-approved rows
    (no fresh cert is issued — use ``/appliances/{id}/rekey`` for
    that). Rejects rows already in ``rejected`` state with 409.
    """
    _require_superadmin(current_user)

    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state == APPLIANCE_STATE_APPROVED:
        return _row_to_schema(row)  # idempotent

    # Lazy CA bootstrap — first approve on a fresh control plane
    # generates the singleton.
    ca = await ensure_ca(db)

    cert_pem, serial_hex, issued_at, expires_at = sign_supervisor_cert(
        ca=ca,
        appliance_id=row.id,
        public_key_der=row.public_key_der,
        public_key_fingerprint=row.public_key_fingerprint,
        hostname=row.hostname,
    )
    row.cert_pem = cert_pem
    row.cert_serial = serial_hex
    row.cert_issued_at = issued_at
    row.cert_expires_at = expires_at
    row.state = APPLIANCE_STATE_APPROVED
    row.approved_at = issued_at
    row.approved_by_user_id = current_user.id
    # Session token cleared — supervisor now uses mTLS. Keep the
    # current value on disk so an in-flight /poll succeeds before the
    # supervisor learns to switch; B3's UI will clarify the transition.
    # Actually — clearing it immediately means the supervisor's
    # in-flight /poll will 403. Leave it for now; the supervisor
    # discards it on its side after receiving cert_pem.

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.approved",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
            new_value={
                "hostname": row.hostname,
                "fingerprint": row.public_key_fingerprint,
                "cert_serial": serial_hex,
                "cert_expires_at": expires_at.isoformat(),
            },
        )
    )
    await db.commit()
    logger.info(
        "appliance_approved",
        appliance_id=str(row.id),
        hostname=row.hostname,
        cert_serial=serial_hex,
        user=current_user.username,
    )
    return _row_to_schema(row)


@router.post(
    "/appliances/{appliance_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Reject a pending appliance (deletes the row, superadmin)",
)
async def reject_appliance(appliance_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    """Reject = DELETE the row. Supervisor's next poll gets 403 +
    falls back to bootstrapping. Distinct from delete (next endpoint)
    only in the audit verb — operationally identical."""
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state == APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot reject an already-approved appliance. Use /delete instead.",
        )
    hostname = row.hostname
    fingerprint = row.public_key_fingerprint
    await db.delete(row)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.rejected",
            resource_type="appliance",
            resource_id=str(appliance_id),
            resource_display=hostname,
            result="success",
            new_value={"fingerprint": fingerprint},
        )
    )
    await db.commit()
    logger.info(
        "appliance_rejected",
        appliance_id=str(appliance_id),
        hostname=hostname,
        user=current_user.username,
    )


@router.delete(
    "/appliances/{appliance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Delete an approved appliance (superadmin)",
)
async def delete_appliance(appliance_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    """Permanently remove an approved appliance from the fleet. The
    supervisor's next mTLS call will fail (cert chain still valid but
    no matching DB row); supervisor falls back to bootstrapping +
    needs a fresh pairing code to re-join."""
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    hostname = row.hostname
    fingerprint = row.public_key_fingerprint
    cert_serial = row.cert_serial
    state_at_delete = row.state
    await db.delete(row)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.deleted",
            resource_type="appliance",
            resource_id=str(appliance_id),
            resource_display=hostname,
            result="success",
            new_value={
                "fingerprint": fingerprint,
                "cert_serial": cert_serial,
                "state_at_delete": state_at_delete,
            },
        )
    )
    await db.commit()
    logger.info(
        "appliance_deleted",
        appliance_id=str(appliance_id),
        hostname=hostname,
        user=current_user.username,
    )


@router.post(
    "/appliances/{appliance_id}/rekey",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Re-sign the appliance's cert (rotation, superadmin)",
)
async def rekey_appliance(
    appliance_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> ApplianceRow:
    """Issue a fresh cert against the existing supervisor pubkey.
    Used for routine 60-day renewal + emergency forced rotation
    (suspected compromise). Subject + SAN identical to the original;
    only the serial + validity window change. Older certs against
    the same pubkey remain technically valid in the CA's eye until
    they expire — Wave-D-or-later CRL work plugs that gap."""
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state != APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot re-key an appliance in state {row.state!r}; approve it first.",
        )

    ca = await ensure_ca(db)
    cert_pem, serial_hex, issued_at, expires_at = sign_supervisor_cert(
        ca=ca,
        appliance_id=row.id,
        public_key_der=row.public_key_der,
        public_key_fingerprint=row.public_key_fingerprint,
        hostname=row.hostname,
    )
    previous_serial = row.cert_serial
    row.cert_pem = cert_pem
    row.cert_serial = serial_hex
    row.cert_issued_at = issued_at
    row.cert_expires_at = expires_at
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.cert_renewed",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
            new_value={
                "previous_cert_serial": previous_serial,
                "new_cert_serial": serial_hex,
                "expires_at": expires_at.isoformat(),
            },
        )
    )
    await db.commit()
    logger.info(
        "appliance_rekeyed",
        appliance_id=str(row.id),
        hostname=row.hostname,
        new_cert_serial=serial_hex,
        previous_cert_serial=previous_serial,
        user=current_user.username,
    )
    return _row_to_schema(row)


# ── Admin: role assignment + tags (#170 Wave C2) ──────────────────


_VALID_ROLES = {"dns-bind9", "dns-powerdns", "dhcp", "observer", "custom"}
_DNS_ROLES = {"dns-bind9", "dns-powerdns"}


class ApplianceRolesUpdate(BaseModel):
    """Operator-driven role + group + tag assignment payload.

    Each field is optional — operator can update a subset without
    blanking the others. ``roles=[]`` is the explicit "idle" signal
    (approved but running nothing); ``roles=None`` (omitted) leaves
    the current role list intact.
    """

    roles: list[str] | None = Field(
        default=None,
        description=(
            "Subset of dns-bind9 / dns-powerdns / dhcp / observer / "
            "custom. dns-bind9 and dns-powerdns are mutually exclusive."
        ),
    )
    dns_group_id: uuid.UUID | None = None
    dhcp_group_id: uuid.UUID | None = None
    tags: dict[str, str] | None = None
    # #170 Wave C3 — operator-pasted nft fragment. ``None`` leaves the
    # current value alone; ``""`` clears it; any other string replaces
    # it. The supervisor runs ``nft -c -f`` against the rendered
    # drop-in before live-swap, so a syntactically invalid value
    # rejects supervisor-side rather than at this endpoint.
    firewall_extra: str | None = None


@router.put(
    "/appliances/{appliance_id}/roles",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Assign roles + groups + tags (superadmin)",
)
async def update_appliance_roles(
    appliance_id: uuid.UUID,
    body: ApplianceRolesUpdate,
    current_user: CurrentUser,
    db: DB,
) -> ApplianceRow:
    """Validate + persist a role assignment. Refuses to assign a role
    the supervisor doesn't advertise the capability for (BIND9 needs
    ``can_run_dns_bind9``, PowerDNS needs ``can_run_dns_powerdns``,
    DHCP needs ``can_run_dhcp``). Refuses the dns-bind9 + dns-powerdns
    combo (one engine per box). 422 on validation failure.

    The supervisor's next heartbeat picks up the change via
    ``role_assignment`` in the response. Service-container lifecycle
    (load image + start / stop / restart) lives on the supervisor —
    this endpoint just records intent.
    """
    _require_superadmin(current_user)

    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state != APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot assign roles to appliance in state {row.state!r}; approve it first.",
        )

    if body.roles is not None:
        # Reject unknown role tokens explicitly so a typo on the API
        # surface doesn't silently roll through the JSONB column.
        for r in body.roles:
            if r not in _VALID_ROLES:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Unknown role {r!r}. Valid: {sorted(_VALID_ROLES)}.",
                )
        # Mutually-exclusive DNS engines.
        dns_engines = _DNS_ROLES.intersection(body.roles)
        if len(dns_engines) > 1:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "dns-bind9 and dns-powerdns are mutually exclusive — one engine per appliance.",
            )
        # Capability gate. Each requested role must be backed by the
        # supervisor's advertised cap. Skips for ``observer`` / ``custom``
        # which don't have a single-flag capability today.
        caps = row.capabilities or {}
        for r in body.roles:
            cap_key = {
                "dns-bind9": "can_run_dns_bind9",
                "dns-powerdns": "can_run_dns_powerdns",
                "dhcp": "can_run_dhcp",
                "observer": "can_run_observer",
            }.get(r)
            if cap_key is not None and not caps.get(cap_key, False):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    (
                        f"Appliance {row.hostname!r} doesn't advertise "
                        f"capability {cap_key}=true; cannot assign role {r!r}."
                    ),
                )
        row.assigned_roles = list(body.roles)

    if body.dns_group_id is not None:
        # Best-effort existence check — wrong group_id → 422.
        from app.models.dns import DNSServerGroup

        dns_group = await db.get(DNSServerGroup, body.dns_group_id)
        if dns_group is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"DNS server group {body.dns_group_id} not found.",
            )
        row.assigned_dns_group_id = dns_group.id
    if body.dhcp_group_id is not None:
        from app.models.dhcp import DHCPServerGroup

        dhcp_group = await db.get(DHCPServerGroup, body.dhcp_group_id)
        if dhcp_group is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"DHCP server group {body.dhcp_group_id} not found.",
            )
        row.assigned_dhcp_group_id = dhcp_group.id

    if body.tags is not None:
        # Coerce every value to string — JSONB will accept anything
        # but the public contract is "string : string" for fleet
        # filters / MCP `tags_match`. Reject non-string values up-front
        # so the operator sees the issue at submit time.
        for k, v in body.tags.items():
            if not isinstance(v, str):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Tag {k!r} must be a string; got {type(v).__name__}.",
                )
        row.tags = dict(body.tags)
    if body.firewall_extra is not None:
        # Empty string is meaningful — operator clearing the extra
        # block. The supervisor renders an empty fragment then; the
        # role-driven mgmt + per-role rules still ship.
        row.firewall_extra = body.firewall_extra if body.firewall_extra else None

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.role_assigned",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
            new_value={
                "roles": list(row.assigned_roles or []),
                "dns_group_id": (
                    str(row.assigned_dns_group_id) if row.assigned_dns_group_id else None
                ),
                "dhcp_group_id": (
                    str(row.assigned_dhcp_group_id) if row.assigned_dhcp_group_id else None
                ),
                "tags": dict(row.tags or {}),
            },
        )
    )
    await db.commit()
    logger.info(
        "appliance_roles_assigned",
        appliance_id=str(row.id),
        hostname=row.hostname,
        roles=list(row.assigned_roles or []),
        user=current_user.username,
    )
    return _row_to_schema(row)


# ── Admin: OS upgrade + reboot (#170 Wave D1) ────────────────────


class ApplianceUpgradeRequest(BaseModel):
    """Stamp the operator's target OS version on an appliance row.

    The supervisor's heartbeat picks this up + writes the
    slot-upgrade trigger on the host. The trigger file's presence
    is the marker the host's systemd ``.path`` unit watches to fire
    ``spatium-upgrade-slot apply``. Idempotent — the supervisor
    skips firing when the trigger already exists or installed
    matches desired.

    Two source modes for the image:

    * **External URL** — pass ``desired_slot_image_url`` directly.
      Used when the appliance can reach github.com or a private
      mirror. The supervisor downloads via the URL on the host.
    * **Uploaded image** — pass ``slot_image_id`` (an
      ``appliance_slot_image`` row id from the air-gap upload
      endpoint). The control plane composes the authenticated
      internal URL the supervisor pulls from. Air-gap-friendly.

    Exactly one of the two must be supplied. The version label is
    always required so the auto-clear logic can detect "installed
    matches desired" without inspecting the binary.
    """

    desired_appliance_version: str = Field(min_length=1, max_length=64)
    desired_slot_image_url: str | None = Field(default=None, min_length=1)
    slot_image_id: uuid.UUID | None = Field(default=None)


@router.post(
    "/appliances/{appliance_id}/upgrade",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Schedule an OS slot upgrade on an Application appliance",
)
async def schedule_appliance_upgrade(
    appliance_id: uuid.UUID,
    body: ApplianceUpgradeRequest,
    request: Request,
    current_user: CurrentUser,
    db: DB,
) -> ApplianceRow:
    """Stamps ``desired_appliance_version`` + ``desired_slot_image_url``
    on the appliance row. The supervisor's next heartbeat picks them up
    and fires the slot-upgrade trigger; the host runner downloads + dd's
    the raw.xz to the inactive slot + reboots into it. Per-row reboot is
    optional — operators who want to defer can clear desired_* via
    ``/clear-upgrade`` before the supervisor's next heartbeat.

    For air-gapped fleets, pass ``slot_image_id`` (an
    ``appliance_slot_image`` row from the upload endpoint) instead of
    ``desired_slot_image_url``. The control plane composes the
    authenticated internal URL the supervisor pulls from."""
    _require_superadmin(current_user)
    if (body.desired_slot_image_url is None) == (body.slot_image_id is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Pass exactly one of desired_slot_image_url or slot_image_id.",
        )
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state != APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot schedule upgrade on appliance in state {row.state!r}.",
        )
    if row.deployment_kind not in (None, "appliance"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            (
                f"Appliance reports deployment_kind={row.deployment_kind!r}; "
                "OS slot upgrades are only available on the SpatiumDDI "
                "appliance OS. Use the manual docker / helm upgrade flow "
                "for docker / k8s deployments."
            ),
        )

    # Resolve slot_image_id → internal URL. We compose the URL
    # relative to the request's host so the supervisor can reach it
    # from the same network it reaches the control plane on; the
    # request's base_url is the operator-facing scheme + host the
    # frontend is already using.
    from app.models.appliance import ApplianceSlotImage  # noqa: PLC0415

    resolved_url: str
    if body.slot_image_id is not None:
        image = await db.get(ApplianceSlotImage, body.slot_image_id)
        if image is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Slot image {body.slot_image_id} not found.",
            )
        # ``request.base_url`` ends with ``/`` and carries the scheme
        # + host the frontend reached us on (X-Forwarded-Host /
        # X-Forwarded-Proto, when nginx is in front). The supervisor
        # is the only thing that resolves this URL, so as long as the
        # frontend host is reachable from the appliance subnet (which
        # it must be — that's where the supervisor already
        # heartbeats), this lines up.
        resolved_url = (
            f"{str(request.base_url).rstrip('/')}"
            f"/api/v1/appliance/slot-images/{image.id}/raw.xz"
        )
    else:
        assert body.desired_slot_image_url is not None
        resolved_url = body.desired_slot_image_url

    row.desired_appliance_version = body.desired_appliance_version
    row.desired_slot_image_url = resolved_url
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.upgrade_scheduled",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
            new_value={
                "desired_appliance_version": body.desired_appliance_version,
                "desired_slot_image_url": resolved_url,
                "slot_image_id": (str(body.slot_image_id) if body.slot_image_id else None),
            },
        )
    )
    await db.commit()
    logger.info(
        "appliance_upgrade_scheduled",
        appliance_id=str(row.id),
        hostname=row.hostname,
        desired_version=body.desired_appliance_version,
        user=current_user.username,
    )
    return _row_to_schema(row)


@router.post(
    "/appliances/{appliance_id}/clear-upgrade",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Clear a pending OS upgrade stamp",
)
async def clear_appliance_upgrade(
    appliance_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> ApplianceRow:
    """Drops ``desired_appliance_version`` + ``desired_slot_image_url``.
    Once the supervisor has already fired the trigger file the host
    runner won't notice this — the slot apply is in flight. The clear
    is most useful when an upgrade was scheduled by mistake and the
    supervisor hasn't heartbeat-polled yet."""
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    row.desired_appliance_version = None
    row.desired_slot_image_url = None
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.upgrade_cleared",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
        )
    )
    await db.commit()
    return _row_to_schema(row)


@router.post(
    "/appliances/{appliance_id}/reboot",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Stamp a reboot request on an Application appliance",
)
async def schedule_appliance_reboot(
    appliance_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> ApplianceRow:
    """Stamps ``reboot_requested=True``. The supervisor's next
    heartbeat returns this; the supervisor writes the host-side
    ``reboot-pending`` trigger; the host runner ``systemctl reboot``s
    after a 5s grace.

    Strict appliance-only — docker / k8s deployments return 409 since
    there's no host to reboot from the supervisor."""
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state != APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot reboot appliance in state {row.state!r}.",
        )
    if row.deployment_kind not in (None, "appliance"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            (
                f"Appliance reports deployment_kind={row.deployment_kind!r}; "
                "host-level reboot is only available on the SpatiumDDI "
                "appliance OS."
            ),
        )
    row.reboot_requested = True
    row.reboot_requested_at = datetime.now(UTC)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.reboot_scheduled",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
        )
    )
    await db.commit()
    logger.info(
        "appliance_reboot_scheduled",
        appliance_id=str(row.id),
        hostname=row.hostname,
        user=current_user.username,
    )
    return _row_to_schema(row)
