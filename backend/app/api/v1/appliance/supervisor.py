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
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

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
from app.core.permissions import is_effective_superadmin, require_permission
from app.models.appliance import (
    APPLIANCE_STATE_APPROVED,
    APPLIANCE_STATE_PENDING_APPROVAL,
    APPLIANCE_STATE_REVOKED,
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
        # Operator-facing presentation can carry dashes / spaces
        # (frontend chunks the code as ``1234-5678`` for
        # readability); strip every non-digit before validating so
        # ``1234-5678`` / ``1234 5678`` / ``12345678`` all
        # resolve to the same canonical hash.
        cleaned = "".join(ch for ch in v if ch.isdigit())
        if len(cleaned) != 8:
            raise ValueError("Pairing code must be 8 decimal digits.")
        return cleaned


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
    # Per-slot installed version — read by the supervisor from the
    # ``slot-versions.json`` sidecar maintained by
    # ``spatium-upgrade-slot sync-versions``. Either / both may be
    # ``None`` (sidecar missing, or freshly imaged inactive slot has
    # never been stamped). The empty-string-mapped values used by the
    # CLI sidecar (``"unstamped"`` / ``"unreadable"`` / ``"unknown"``)
    # are accepted verbatim — the UI's ``slotVersion`` helper renders
    # them as ``"—"``.
    slot_a_version: str | None = None
    slot_b_version: str | None = None
    is_trial_boot: bool | None = None
    last_upgrade_state: Literal["ready", "in-flight", "done", "failed"] | None = None
    last_upgrade_state_at: datetime | None = None
    snmpd_running: bool | None = None
    ntp_sync_state: Literal["synchronized", "unsynchronized", "unknown"] | None = None
    # #170 Phase E2 — host-side port conflicts probed by the
    # supervisor. Free-form dict keyed by ``<proto>_<port>``; values
    # are the ``users`` string from ``ss -p`` (process name / pid).
    # None / omitted = supervisor didn't run the probe this tick.
    port_conflicts: dict[str, str] | None = None
    # #170 Wave D follow-up — outcome of the supervisor's last
    # compose-lifecycle apply (``idle`` / ``ready`` / ``failed``).
    # None on the first heartbeat / before any role assignment.
    role_switch_state: Literal["idle", "ready", "failed"] | None = None
    role_switch_reason: str | None = None
    # #170 Wave E — service-container watchdog. Free-form dict keyed
    # by compose service name (``dns-bind9`` / ``dns-powerdns`` /
    # ``dhcp-kea``); each value carries ``{role, status, since,
    # container_id}`` where status ∈ {healthy, missing, unhealthy,
    # starting}. The supervisor probes every 5 min and ships the
    # cached verdict on every heartbeat; the Fleet drilldown surfaces
    # the per-service status alongside the role-assignment block.
    # None / omitted = supervisor didn't run the watchdog this tick
    # (typical on docker / k8s deployments or before the first probe).
    role_health: dict[str, dict[str, Any]] | None = None
    # Issue #183 Phase 4 — local k3s cluster health summary. Shape:
    # ``{"kubeapi_ready": bool, "nodes_total": int, "nodes_ready":
    # int, "pods_total": int, "pods_by_phase": {<phase>: count}}``.
    # Empty dict on legacy compose appliances; None / omitted = the
    # supervisor didn't run the probe this tick (pre-#183 supervisors).
    cluster_health: dict[str, Any] | None = None
    # Issue #183 Phase 5 — operator-facing k3s metadata.
    # ``k3s_version`` is the upstream release tag the slot was baked
    # against (e.g. ``v1.35.4+k3s1``). ``kubeconfig`` is the raw
    # admin kubeconfig YAML straight off the appliance — the backend
    # rewrites ``server:`` for operator reachability + Fernet-encrypts
    # before persisting. Both ``None`` = supervisor didn't ship them
    # this tick (legacy compose / pre-Phase-5 / k3s not yet started).
    k3s_version: str | None = None
    kubeconfig: str | None = None
    # Issue #183 Phase 6 — k3s server-cert ``Not After`` timestamp
    # (ISO-8601 UTC). None when not k3s; the heartbeat handler
    # leaves the column untouched in that case.
    k3s_api_cert_expires_at: datetime | None = None


class SupervisorRoleAssignment(BaseModel):
    """Per-role config block the supervisor needs to bring up a
    service container (#170 Wave C2).

    DNS / DHCP groups carry an ``agent_key`` so the service container
    can register against the existing ``/dns/agents/register`` /
    ``/dhcp/agents/register`` endpoints. The key is the platform-level
    bootstrap PSK the operator already revealed under Settings →
    Security; the supervisor passes it into the service container's
    env via ``role-compose.env`` so the operator doesn't have to SSH
    in and edit the appliance's ``/etc/spatiumddi/.env`` by hand.

    Returned only when the corresponding role is in the appliance's
    ``assigned_roles`` list — a DHCP-only appliance doesn't get the
    DNS key, even though both PSKs are global.
    """

    roles: list[str] = Field(default_factory=list)
    dns_group_id: uuid.UUID | None = None
    dns_group_name: str | None = None
    dns_engine: str | None = None  # bind9 / powerdns
    # #170 Wave D follow-up — platform-level DNS agent PSK (mirror of
    # ``settings.dns_agent_key``). Populated when a DNS role is
    # assigned + the control plane has the key configured; ``None``
    # otherwise. The supervisor writes ``DNS_AGENT_KEY=<this>`` into
    # role-compose.env so the bind9 / powerdns service container can
    # register against ``/api/v1/dns/agents/register`` without
    # operator-side .env edits.
    dns_agent_key: str | None = None
    dhcp_group_id: uuid.UUID | None = None
    dhcp_group_name: str | None = None
    dhcp_network_mode: str | None = None  # host / bridged
    dhcp_agent_key: str | None = None
    # #170 Wave C3 — operator-pasted nft fragment rendered after the
    # role-driven mgmt + per-role blocks. NULL / empty → role-driven
    # rules only.
    firewall_extra: str | None = None
    # Issue #183 Phase 6 — operator-allowed CIDRs for direct kubeapi
    # access on tcp/6443. Empty = proxy-only (kubeapi stays on
    # 127.0.0.1, only the supervisor's outbound proxy channel can
    # drive it). The supervisor's firewall renderer emits one
    # ``ip saddr { ... } tcp dport 6443 accept`` rule per heartbeat.
    kubeapi_expose_cidrs: list[str] = Field(default_factory=list)


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
    # Operator's per-slot boot intents. The supervisor writes the
    # matching trigger file when non-null + the current state doesn't
    # already satisfy the request. Both auto-clear in the heartbeat
    # handler once the supervisor reports back that the action landed.
    #
    # ``desired_next_boot_slot`` = one-shot (``grub-reboot``, reverts
    # on the NEXT reboot if the operator doesn't commit). Used to
    # test an inactive slot. Cleared once the supervisor reports the
    # target slot as ``current_slot``.
    # ``desired_default_slot`` = durable (``grub-set-default``,
    # survives reboots). Used to commit a trial boot, or to durably
    # revert. Cleared once the supervisor reports the target slot as
    # ``durable_default``.
    desired_next_boot_slot: Literal["slot_a", "slot_b"] | None = None
    desired_default_slot: Literal["slot_a", "slot_b"] | None = None
    reboot_requested: bool = False
    # #170 Wave D follow-up — supervisor's signed cert + CA chain.
    # Populated when the appliance has been approved + the supervisor
    # hasn't picked them up yet. The supervisor saves them to
    # /var/persist/spatium-supervisor/tls/ + switches its next
    # heartbeat to cert auth (X-Appliance-Cert + X-Appliance-Signature
    # + X-Appliance-Timestamp). Same fields the legacy /supervisor/poll
    # response carried; in-lining them here lets the supervisor stop
    # polling separately.
    cert_pem: str | None = None
    ca_chain_pem: str | None = None
    cert_expires_at: datetime | None = None
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

    # #170 Wave D follow-up — cert auth takes precedence when the
    # supervisor presents X-Appliance-Cert + X-Appliance-Signature +
    # X-Appliance-Timestamp headers. The session-token path remains
    # as the fallback for pending_approval rows (which don't have a
    # cert yet).
    from app.services.appliance.cert_auth import (  # noqa: PLC0415
        CertAuthFailed,
        authenticate_cert,
    )

    cert_principal = None
    try:
        cert_principal = await authenticate_cert(request, db)
    except CertAuthFailed as exc:
        await asyncio.sleep(_CONSUME_FAILURE_DELAY_S)
        logger.warning(
            "supervisor_heartbeat_cert_auth_failed",
            appliance_id=str(body.appliance_id),
            reason=exc.reason,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid appliance client cert.")

    if cert_principal is not None:
        # Cert subject CN must match the body's appliance_id (defence-
        # in-depth: a supervisor with cert for A can't post telemetry
        # claiming to be B).
        if cert_principal.appliance.id != body.appliance_id:
            await asyncio.sleep(_CONSUME_FAILURE_DELAY_S)
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Cert subject mismatch.",
            )
        row = cert_principal.appliance
    else:
        # Session-token fallback for pending_approval rows.
        row = await db.get(Appliance, body.appliance_id)
        valid = row is not None and (
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
    # Issue #170 Wave E follow-up — reject heartbeats from soft-deleted
    # appliances even when the cert chain still validates. The
    # supervisor's three-strike detector turns the 403 into local
    # ``revoked`` state + tears down service containers. Keeps the
    # cert-auth path simple (still trust the cert) while honouring
    # the operator's soft-delete intent at the API boundary.
    if row.state == APPLIANCE_STATE_REVOKED:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Appliance has been revoked. Re-authorize on the Fleet page to restore.",
        )

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
    if body.slot_a_version is not None:
        row.slot_a_version = body.slot_a_version
    if body.slot_b_version is not None:
        row.slot_b_version = body.slot_b_version
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
    if body.port_conflicts is not None:
        # Overwrite verbatim — the supervisor's probe is the source of
        # truth. Empty dict explicitly clears prior conflicts; the
        # operator-visible banner stops showing on the next render.
        row.port_conflicts = dict(body.port_conflicts)
    if body.role_switch_state is not None:
        row.role_switch_state = body.role_switch_state
        # ``reason`` is paired with the state — clear it on idle /
        # ready (the prior failure is moot) so the Fleet UI doesn't
        # carry stale red text once the operator fixes the cause.
        if body.role_switch_state == "failed":
            row.role_switch_reason = body.role_switch_reason
        else:
            row.role_switch_reason = None
    if body.role_health is not None:
        # #170 Wave E — supervisor's per-service watchdog verdict.
        # Overwrite verbatim every tick: empty dict clears stale
        # entries when the operator removes a role, and the
        # supervisor's ``since`` timestamp is the canonical "first
        # observed in this status" anchor across heartbeats.
        row.role_health = dict(body.role_health)
    if body.cluster_health is not None:
        # Issue #183 Phase 4 — supervisor's local-k3s health summary.
        # Same overwrite-verbatim shape as role_health. Empty dict
        # is a meaningful signal (legacy compose; clear stale state).
        row.cluster_health = dict(body.cluster_health)

    # Issue #183 Phase 5 — k3s version + kubeconfig persist. Both
    # follow "only update when not None" so legacy compose / pre-
    # Phase-5 supervisors don't blank the columns out.
    if body.k3s_version is not None:
        row.k3s_version = body.k3s_version
    if body.k3s_api_cert_expires_at is not None:
        # Issue #183 Phase 6 — k3s serving-cert expiry. Drives the
        # ``k3s_api_cert_expiring`` alert rule. Overwrite-verbatim so
        # k3s cert rotation (1-year default) propagates on the next
        # tick.
        row.k3s_api_cert_expires_at = body.k3s_api_cert_expires_at
    if body.kubeconfig is not None:
        from app.core.crypto import encrypt_str  # noqa: PLC0415

        # Rewrite ``server: https://127.0.0.1:6443`` → the appliance's
        # last-seen IP so the operator's downloaded kubeconfig
        # actually works against the appliance over the wire. Falls
        # back to localhost when last_seen_ip is unknown (operator
        # can edit themselves). Same IP we surface in the row chip.
        rewritten = body.kubeconfig
        if row.last_seen_ip:
            # k3s.yaml's server line is structured + greppable; the
            # supervisor doesn't run a port-7443 listener so 6443 is
            # always the right target port.
            new_server = f"server: https://{row.last_seen_ip}:6443"
            rewritten = re.sub(
                r"server:\s*https://127\.0\.0\.1:6443",
                new_server,
                rewritten,
            )
            rewritten = re.sub(
                r"server:\s*https://0\.0\.0\.0:6443",
                new_server,
                rewritten,
            )
        row.kubeconfig_encrypted = encrypt_str(rewritten)

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

    # Auto-clear desired_next_boot_slot once the supervisor reports
    # the requested slot as ``current_slot`` — the operator's intent
    # was "boot into this slot next", and we got there. ``durable_
    # default`` is irrelevant here (next-boot is one-shot by design).
    if row.desired_next_boot_slot is not None and row.current_slot == row.desired_next_boot_slot:
        row.desired_next_boot_slot = None

    # Auto-clear desired_default_slot once the supervisor reports the
    # requested slot as ``durable_default`` — grub-set-default landed
    # and survives subsequent reboots.
    if row.desired_default_slot is not None and row.durable_default == row.desired_default_slot:
        row.desired_default_slot = None

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

    # #170 Wave D follow-up — include cert + CA chain so the
    # supervisor picks them up on the heartbeat right after approval
    # + switches to cert auth on subsequent calls. Always emit them
    # for approved rows (idempotent on the supervisor side — re-saving
    # the same bytes is a no-op); pending_approval rows have NULL
    # cert_pem so the field stays None.
    ca_chain_pem: str | None = None
    if row.cert_pem is not None:
        ca = await ensure_ca(db)
        ca_chain_pem = ca.cert_pem

    return SupervisorHeartbeatResponse(
        appliance_id=row.id,
        state=row.state,  # type: ignore[arg-type]
        desired_appliance_version=row.desired_appliance_version,
        desired_slot_image_url=row.desired_slot_image_url,
        desired_next_boot_slot=row.desired_next_boot_slot,  # type: ignore[arg-type]
        desired_default_slot=row.desired_default_slot,  # type: ignore[arg-type]
        reboot_requested=row.reboot_requested,
        cert_pem=row.cert_pem,
        ca_chain_pem=ca_chain_pem,
        cert_expires_at=row.cert_expires_at,
        role_assignment=role_assignment,
    )


async def _build_role_assignment(db: DB, row: Appliance) -> SupervisorRoleAssignment:
    """Resolve the assigned roles + group identities for a supervisor
    heartbeat response. Best-effort — a missing group falls through
    as a null group_id; the supervisor treats that as "skip this
    role" (idle on the affected service)."""
    from app.config import settings as app_settings
    from app.models.dhcp import DHCPServerGroup
    from app.models.dns import DNSServerGroup

    assigned_roles = list(row.assigned_roles or [])
    dns_role_assigned = "dns-bind9" in assigned_roles or "dns-powerdns" in assigned_roles
    dhcp_role_assigned = "dhcp" in assigned_roles

    dns_engine: str | None = None
    if "dns-bind9" in assigned_roles:
        dns_engine = "bind9"
    elif "dns-powerdns" in assigned_roles:
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

    # #170 Wave D follow-up — only ship the bootstrap PSK to
    # supervisors whose appliance has the matching role assigned.
    # An observer-only appliance gets neither; a DNS-only appliance
    # gets only the DNS key. Keeps blast radius bounded if a single
    # supervisor cert leaks.
    dns_agent_key: str | None = None
    if dns_role_assigned:
        dns_agent_key = app_settings.dns_agent_key or None
    dhcp_agent_key: str | None = None
    if dhcp_role_assigned:
        dhcp_agent_key = app_settings.dhcp_agent_key or None

    return SupervisorRoleAssignment(
        roles=assigned_roles,
        dns_group_id=row.assigned_dns_group_id,
        dns_group_name=dns_group_name,
        dns_engine=dns_engine,
        dns_agent_key=dns_agent_key,
        dhcp_group_id=row.assigned_dhcp_group_id,
        dhcp_group_name=dhcp_group_name,
        dhcp_network_mode=dhcp_network_mode,
        dhcp_agent_key=dhcp_agent_key,
        firewall_extra=row.firewall_extra,
        kubeapi_expose_cidrs=list(row.kubeapi_expose_cidrs or []),
    )


# ── Admin: approve / reject / delete / re-key ─────────────────────


class ApplianceRow(BaseModel):
    """Operator-facing summary of an appliance row. Cert / pubkey
    bytes are not exposed — only their derived metadata."""

    id: uuid.UUID
    hostname: str
    state: Literal["pending_approval", "approved", "rejected", "revoked"]
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
    slot_a_version: str | None
    slot_b_version: str | None
    is_trial_boot: bool
    last_upgrade_state: str | None
    last_upgrade_state_at: datetime | None
    snmpd_running: bool | None
    ntp_sync_state: str | None
    desired_appliance_version: str | None
    desired_slot_image_url: str | None
    desired_next_boot_slot: str | None
    desired_default_slot: str | None
    reboot_requested: bool
    reboot_requested_at: datetime | None
    # #170 Wave C2 — role assignment + free-form tags.
    assigned_roles: list[str]
    assigned_dns_group_id: uuid.UUID | None
    assigned_dhcp_group_id: uuid.UUID | None
    tags: dict[str, str]
    # #170 Wave C3 — operator-pasted nft fragment.
    firewall_extra: str | None
    # #170 Phase E2 — supervisor-reported host-side port conflicts.
    port_conflicts: dict[str, str]
    # #170 Wave D follow-up — outcome of the supervisor's last
    # compose-lifecycle apply.
    role_switch_state: str | None
    role_switch_reason: str | None
    # #170 Wave E — service-container watchdog. Per-service health
    # the supervisor reports every 5 min via heartbeat. Keys are
    # compose service names (``dns-bind9`` / ``dns-powerdns`` /
    # ``dhcp-kea``); values carry ``{role, status, since,
    # container_id}``.
    role_health: dict[str, dict[str, Any]]
    # Issue #183 Phase 4 — local k3s cluster health summary.
    cluster_health: dict[str, Any]
    # Issue #183 Phase 5 — installed k3s version (plain text, public).
    # NULL on legacy compose appliances / pre-#183 supervisors.
    k3s_version: str | None
    # Issue #183 Phase 5 — boolean indicating whether the supervisor
    # has shipped a kubeconfig the operator can reveal. The cipher-
    # text itself never leaves the server outside the reveal endpoint;
    # the row schema only exposes a "have I got one" bit.
    kubeconfig_set: bool
    # Issue #183 Phase 6 — k3s server-cert ``Not After`` timestamp.
    # NULL on legacy compose appliances; drives the
    # ``k3s_api_cert_expiring`` alert rule.
    k3s_api_cert_expires_at: datetime | None
    # Issue #183 Phase 6 — operator-controlled CIDR allowlist for
    # direct kubeapi access on tcp/6443. Empty = proxy-only.
    kubeapi_expose_cidrs: list[str]
    # #170 Wave E follow-up — soft-delete timestamp. Non-null on
    # ``state=revoked`` rows; cleared by re-authorize.
    revoked_at: datetime | None = None
    created_at: datetime


class ApplianceList(BaseModel):
    appliances: list[ApplianceRow]


def _require_superadmin(user: CurrentUser) -> None:
    if not is_effective_superadmin(user):
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
        slot_a_version=row.slot_a_version,
        slot_b_version=row.slot_b_version,
        is_trial_boot=row.is_trial_boot,
        last_upgrade_state=row.last_upgrade_state,
        last_upgrade_state_at=row.last_upgrade_state_at,
        snmpd_running=row.snmpd_running,
        ntp_sync_state=row.ntp_sync_state,
        desired_appliance_version=row.desired_appliance_version,
        desired_slot_image_url=row.desired_slot_image_url,
        desired_next_boot_slot=row.desired_next_boot_slot,
        desired_default_slot=row.desired_default_slot,
        reboot_requested=row.reboot_requested,
        reboot_requested_at=row.reboot_requested_at,
        assigned_roles=list(row.assigned_roles or []),
        assigned_dns_group_id=row.assigned_dns_group_id,
        assigned_dhcp_group_id=row.assigned_dhcp_group_id,
        tags=dict(row.tags or {}),
        firewall_extra=row.firewall_extra,
        port_conflicts=dict(row.port_conflicts or {}),
        role_switch_state=row.role_switch_state,
        role_switch_reason=row.role_switch_reason,
        role_health=dict(row.role_health or {}),
        cluster_health=dict(row.cluster_health or {}),
        k3s_version=row.k3s_version,
        kubeconfig_set=row.kubeconfig_encrypted is not None,
        k3s_api_cert_expires_at=row.k3s_api_cert_expires_at,
        kubeapi_expose_cidrs=list(row.kubeapi_expose_cidrs or []),
        revoked_at=row.revoked_at,
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


class DeleteApplianceRequest(BaseModel):
    """Body for ``DELETE /appliances/{id}`` — operator's current
    password gates the destructive action. Same shape as the
    factory-reset re-auth and the agent-bootstrap-key reveal: the
    server verifies against the caller's ``hashed_password`` so a
    leaked session token alone can't drop fleet rows."""

    password: str = Field(min_length=1, max_length=256)


@router.delete(
    "/appliances/{appliance_id}",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Soft-delete an appliance (superadmin + password re-auth)",
)
async def delete_appliance(
    appliance_id: uuid.UUID,
    body: DeleteApplianceRequest,
    current_user: CurrentUser,
    db: DB,
) -> ApplianceRow:
    """Soft-delete an appliance (#170 Wave E follow-up).

    The row stays — ``state`` flips to ``revoked`` + ``revoked_at``
    stamped — so heartbeats from that appliance return 403, the
    supervisor's revocation detector trips, and its DNS/DHCP service
    containers tear down. An admin can later either
    ``POST /appliances/{id}/reauthorize`` (flip back to ``approved``
    + clear ``revoked_at``) or
    ``POST /appliances/{id}/permanent-delete`` (real DELETE).

    A Celery beat sweep eventually hard-deletes rows whose
    ``revoked_at`` is older than
    ``platform_settings.appliance_revoked_retention_days`` (default
    30, ``0`` disables auto-purge).

    Requires the operator's current password — UI mis-click guard
    even with the per-row Delete button + checkbox.
    """
    from app.core.security import verify_password  # noqa: PLC0415

    _require_superadmin(current_user)
    if not current_user.hashed_password or not verify_password(
        body.password, current_user.hashed_password
    ):
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="appliance.delete_denied",
                resource_type="appliance",
                resource_id=str(appliance_id),
                result="denied",
                error_message="bad_password",
            )
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Current password incorrect.",
        )
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    state_at_delete = row.state
    row.state = APPLIANCE_STATE_REVOKED
    row.revoked_at = datetime.now(UTC)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.soft_deleted",
            resource_type="appliance",
            resource_id=str(appliance_id),
            resource_display=row.hostname,
            result="success",
            new_value={
                "state_at_delete": state_at_delete,
                "revoked_at": row.revoked_at.isoformat(),
            },
        )
    )
    await db.commit()
    await db.refresh(row)
    logger.info(
        "appliance_soft_deleted",
        appliance_id=str(appliance_id),
        hostname=row.hostname,
        user=current_user.username,
    )
    return _row_to_schema(row)


@router.post(
    "/appliances/{appliance_id}/reauthorize",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Re-authorize a revoked appliance (superadmin)",
)
async def reauthorize_appliance(
    appliance_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
) -> ApplianceRow:
    """Lift a soft-delete: clear ``revoked_at`` and put the appliance
    back in ``approved``. The supervisor's three-strike detector will
    self-clear on the next 200 heartbeat — but bringing service
    containers BACK up requires the operator to re-fire the role
    assignment (the supervisor's revoke-teardown ran a ``compose stop``;
    a fresh role-assignment apply re-runs ``up -d``).
    """
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state != APPLIANCE_STATE_REVOKED:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Appliance is in state {row.state!r}; only revoked rows can be re-authorized.",
        )
    row.state = APPLIANCE_STATE_APPROVED
    row.revoked_at = None
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.reauthorized",
            resource_type="appliance",
            resource_id=str(appliance_id),
            resource_display=row.hostname,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(row)
    logger.info(
        "appliance_reauthorized",
        appliance_id=str(appliance_id),
        hostname=row.hostname,
        user=current_user.username,
    )
    return _row_to_schema(row)


@router.post(
    "/appliances/{appliance_id}/permanent-delete",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Permanently delete an appliance row (superadmin + password)",
)
async def permanent_delete_appliance(
    appliance_id: uuid.UUID,
    body: DeleteApplianceRequest,
    current_user: CurrentUser,
    db: DB,
) -> None:
    """Hard DELETE the appliance row. Same password gate as the soft
    delete + an explicit state check: the row must already be
    ``revoked`` (operator went through soft-delete first). This is
    the recovery path for after-the-fact "yes I'm sure" + the action
    invoked by the retention sweep when ``revoked_at`` is older than
    the retention window.
    """
    from app.core.security import verify_password  # noqa: PLC0415

    _require_superadmin(current_user)
    if not current_user.hashed_password or not verify_password(
        body.password, current_user.hashed_password
    ):
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="appliance.permanent_delete_denied",
                resource_type="appliance",
                resource_id=str(appliance_id),
                result="denied",
                error_message="bad_password",
            )
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Current password incorrect.",
        )
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state != APPLIANCE_STATE_REVOKED:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Permanent delete is only allowed on revoked appliances; soft-delete first.",
        )
    hostname = row.hostname
    fingerprint = row.public_key_fingerprint
    cert_serial = row.cert_serial
    await db.delete(row)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.permanently_deleted",
            resource_type="appliance",
            resource_id=str(appliance_id),
            resource_display=hostname,
            result="success",
            new_value={
                "fingerprint": fingerprint,
                "cert_serial": cert_serial,
            },
        )
    )
    await db.commit()
    logger.info(
        "appliance_permanently_deleted",
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
        #
        # ``?t=<hmac>`` token authorises the host-side
        # ``spatium-upgrade-slot`` runner — it does an unauthenticated
        # ``urllib.request.urlopen`` because it has no operator
        # session and no mTLS material. The token is HMAC'd against
        # the image_id + SECRET_KEY, so a leaked URL can't be replayed
        # against a different image.
        from app.api.v1.appliance.slot_images import (  # noqa: PLC0415
            slot_image_download_token,
        )

        token = slot_image_download_token(image.id)
        resolved_url = (
            f"{str(request.base_url).rstrip('/')}"
            f"/api/v1/appliance/slot-images/{image.id}/raw.xz?t={token}"
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


class ApplianceSlotActionRequest(BaseModel):
    """Pick which A/B slot the operator wants to act on.

    Used by both ``/set-next-boot`` (one-shot, ``grub-reboot``) and
    ``/set-default-slot`` (durable, ``grub-set-default``). The
    supervisor's next heartbeat picks the desired field up and writes
    the matching trigger file the host runner watches.
    """

    slot: Literal["slot_a", "slot_b"]


def _check_appliance_slot_action_allowed(row: Appliance) -> None:
    """Shared guards for both ``/set-next-boot`` + ``/set-default-slot``.

    A slot action only makes sense on an approved appliance host: a
    docker / k8s row has no A/B partition layout, and a pending /
    revoked / rejected row is offline to the supervisor heartbeat
    cycle that delivers the intent.
    """
    if row.state != APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot change boot slot on appliance in state {row.state!r}.",
        )
    if row.deployment_kind not in (None, "appliance"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            (
                f"Appliance reports deployment_kind={row.deployment_kind!r}; "
                "A/B slot operations are only available on the SpatiumDDI "
                "appliance OS."
            ),
        )


@router.post(
    "/appliances/{appliance_id}/set-next-boot",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Boot a specific A/B slot on the next reboot (one-shot)",
)
async def schedule_appliance_set_next_boot(
    appliance_id: uuid.UUID,
    body: ApplianceSlotActionRequest,
    current_user: CurrentUser,
    db: DB,
) -> ApplianceRow:
    """Stamps ``desired_next_boot_slot`` on the appliance row. The
    supervisor's heartbeat picks it up + writes the
    ``slot-set-next-boot-pending`` trigger; the host runner invokes
    ``spatium-upgrade-slot set-next-boot <slot>`` (``grub-reboot``).

    Semantics: one-shot. The slot is set for exactly the next boot.
    If the operator doesn't commit (via ``/set-default-slot``) before
    the boot AFTER that, grub auto-reverts to the previous durable
    default — that's the safety net behind trial-boot upgrades.

    The actual reboot is NOT triggered by this call. Operator
    either reboots manually (``/reboot`` endpoint) or waits for the
    next planned reboot window."""
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    _check_appliance_slot_action_allowed(row)
    row.desired_next_boot_slot = body.slot
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.set_next_boot_scheduled",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
            new_value={"desired_next_boot_slot": body.slot},
        )
    )
    await db.commit()
    logger.info(
        "appliance_set_next_boot_scheduled",
        appliance_id=str(row.id),
        hostname=row.hostname,
        slot=body.slot,
        user=current_user.username,
    )
    return _row_to_schema(row)


@router.post(
    "/appliances/{appliance_id}/set-default-slot",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Durably set the default A/B boot slot (commit / revert)",
)
async def schedule_appliance_set_default_slot(
    appliance_id: uuid.UUID,
    body: ApplianceSlotActionRequest,
    current_user: CurrentUser,
    db: DB,
) -> ApplianceRow:
    """Stamps ``desired_default_slot`` on the appliance row. The
    supervisor's heartbeat picks it up + writes the
    ``slot-set-default-pending`` trigger; the host runner invokes
    ``spatium-upgrade-slot set-default <slot>``
    (``grub-set-default``).

    Semantics: durable. The grub default flips and survives subsequent
    reboots. Two common uses:

    * **Commit a trial boot** — operator booted the trial slot via
      ``/set-next-boot``, validated it, calls this against the
      currently-running slot to make it durable. (``firstboot.service``
      also does this automatically when ``/health/live`` passes; this
      endpoint exists for the explicit operator-action case.)
    * **Durable revert** — operator wants to go back to the previous
      slot for good (not just one boot). Calls this against the
      previous slot."""
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    _check_appliance_slot_action_allowed(row)
    row.desired_default_slot = body.slot
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.set_default_slot_scheduled",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
            new_value={"desired_default_slot": body.slot},
        )
    )
    await db.commit()
    logger.info(
        "appliance_set_default_slot_scheduled",
        appliance_id=str(row.id),
        hostname=row.hostname,
        slot=body.slot,
        user=current_user.username,
    )
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


# ── Issue #183 Phase 4 — kubeapi proxy + restart-deployment action ──


class K8sProxyPollResponse(BaseModel):
    """Long-poll result. ``request_id`` is empty + ``method`` is empty
    when the poll timed out without a request — the supervisor handles
    that as "no work, poll again". Otherwise the supervisor decodes
    ``body_b64`` and POSTs against its local kubeapi."""

    request_id: str
    method: str
    path: str
    headers: dict[str, str]
    body_b64: str


class K8sProxyReplyRequest(BaseModel):
    """Supervisor-sent reply after executing the proxied request
    against the local kubeapi. ``status`` is the kubeapi's HTTP
    status; ``body_b64`` carries the response body verbatim."""

    request_id: str
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body_b64: str = ""


async def _require_cert_auth(request: Request, db: DB) -> Appliance:
    """Shared cert-auth gate for the proxy endpoints. Returns the
    authenticated appliance row. Raises 403 on failure — no fallback
    to session-token here since the proxy channel only exists for
    approved appliances with mTLS certs.
    """
    from app.services.appliance.cert_auth import (  # noqa: PLC0415
        CertAuthFailed,
        authenticate_cert,
    )

    try:
        principal = await authenticate_cert(request, db)
    except CertAuthFailed as exc:
        logger.warning("appliance.k8s_proxy.cert_auth_failed", reason=exc.reason)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid appliance client cert.") from exc
    if principal is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Cert headers required for kubeapi proxy.",
        )
    if principal.appliance.state != APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Appliance in state {principal.appliance.state!r}; proxy disabled.",
        )
    return principal.appliance


@router.post(
    "/supervisor/k8s-proxy/poll",
    response_model=K8sProxyPollResponse,
    summary="Long-poll for the next queued kubeapi request",
)
async def k8s_proxy_poll(request: Request, db: DB) -> K8sProxyPollResponse:
    """Supervisor-only endpoint. The supervisor's ``k8s_proxy.py``
    background thread holds an outbound long-poll here; when an
    operator action enqueues a request bound for this appliance, the
    poll returns immediately. Otherwise the request times out after
    30 s and the supervisor re-issues.

    Cert auth required — anonymous callers can't intercept queued
    requests. Cert subject must match an approved appliance row
    (the proxy queue is keyed by appliance_id, so a misbehaving cert
    would only see its own queue anyway).
    """
    from app.services.appliance import k8s_proxy as _proxy  # noqa: PLC0415

    appliance = await _require_cert_auth(request, db)
    queued = await _proxy.pop_request(appliance.id, timeout=30.0)
    if queued is None:
        # No request within the timeout — return an empty shape so
        # the supervisor loop just re-polls. 200 (not 204) so the
        # supervisor doesn't have to special-case "no body".
        return K8sProxyPollResponse(request_id="", method="", path="", headers={}, body_b64="")
    return K8sProxyPollResponse(
        request_id=queued.request_id,
        method=queued.method,
        path=queued.path,
        headers=queued.headers,
        body_b64=queued.body_b64,
    )


@router.post(
    "/supervisor/k8s-proxy/reply/{request_id}",
    summary="Return a kubeapi response to the awaiting operator action",
)
async def k8s_proxy_reply(
    request_id: str,
    body: K8sProxyReplyRequest,
    request: Request,
    db: DB,
) -> dict[str, str]:
    """Supervisor-only endpoint. Once the supervisor has executed the
    proxied request against the local kubeapi, it POSTs the response
    here. The backend's in-memory future map matches the request_id
    + resolves the operator action's pending future.

    Returns 200 either way — late replies (operator already timed
    out + the future was GC'd) are logged + discarded server-side
    so the supervisor's loop stays simple.
    """
    from app.services.appliance import k8s_proxy as _proxy  # noqa: PLC0415

    appliance = await _require_cert_auth(request, db)
    if body.request_id != request_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "request_id path/body mismatch",
        )
    response = _proxy.K8sProxyResponse(
        request_id=request_id,
        status=body.status,
        headers=body.headers,
        body_b64=body.body_b64,
    )
    delivered = _proxy.deliver_response(response)
    logger.info(
        "appliance.k8s_proxy.reply",
        appliance_id=str(appliance.id),
        request_id=request_id,
        status=body.status,
        delivered=delivered,
    )
    return {"delivered": "true" if delivered else "stale"}


class ApplianceRestartDeploymentRequest(BaseModel):
    """Operator-driven Deployment / DaemonSet rollout-restart.

    Same effect as ``kubectl rollout restart <kind>/<name> -n
    <namespace>``. Bumps the pod template's
    ``kubectl.kubernetes.io/restartedAt`` annotation, which makes the
    controller spin up new pods and reap the old ones one at a time.
    """

    kind: Literal["Deployment", "DaemonSet"]
    namespace: str = Field(default="spatium", min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=253)


@router.post(
    "/appliances/{appliance_id}/k8s/restart",
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Rollout-restart a Deployment/DaemonSet on the appliance's k3s",
)
async def k8s_rollout_restart(
    appliance_id: uuid.UUID,
    body: ApplianceRestartDeploymentRequest,
    current_user: CurrentUser,
    db: DB,
) -> dict[str, object]:
    """First operator-facing direct-kubeapi action (#183 Phase 4
    proof-of-concept). Enqueues a kubeapi PATCH against the local
    cluster via the supervisor proxy + waits up to 30 s for the
    response.

    Returns ``{"ok": true, "status": <kubeapi-status>}`` on success;
    surfaces a 504 if the supervisor doesn't reply in time, 502 if
    the kubeapi itself returned an error.
    """
    from app.services.appliance import k8s_proxy as _proxy  # noqa: PLC0415

    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state != APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Appliance in state {row.state!r}; kubeapi proxy unavailable.",
        )

    # Strategic-merge patch that bumps the pod template's
    # ``restartedAt`` annotation. Standard kubectl rollout-restart
    # shape — same JSON kubectl sends.
    api_path = f"/apis/apps/v1/namespaces/{body.namespace}/" f"{body.kind.lower()}s/{body.name}"
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": datetime.now(UTC).isoformat()
                    }
                }
            }
        }
    }
    try:
        status_code, response_body = await _proxy.k8s_call(
            row.id,
            "PATCH",
            api_path,
            body=patch_body,
            content_type="application/strategic-merge-patch+json",
            timeout=20.0,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "Supervisor didn't reply in 20s. Is the appliance heartbeating?",
        ) from exc

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.k8s_rollout_restart",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success" if 200 <= status_code < 300 else "failed",
            new_value={
                "kind": body.kind,
                "namespace": body.namespace,
                "name": body.name,
                "kubeapi_status": status_code,
            },
        )
    )
    await db.commit()

    if not 200 <= status_code < 300:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"kubeapi returned {status_code}: {response_body[:200]!r}",
        )

    logger.info(
        "appliance.k8s_rollout_restart",
        appliance_id=str(row.id),
        kind=body.kind,
        namespace=body.namespace,
        name=body.name,
        kubeapi_status=status_code,
        user=current_user.username,
    )
    return {"ok": True, "status": status_code, "kind": body.kind, "name": body.name}


# ── Issue #183 Phase 5 — kubeconfig reveal ──


class RevealKubeconfigRequest(BaseModel):
    """Operator's local-auth password — re-verified before we hand
    back the cleartext kubeconfig. Same gate the SNMP-community and
    agent-bootstrap-key reveals use."""

    password: str


class RevealKubeconfigResponse(BaseModel):
    """Payload of a successful reveal. ``hostname`` is the operator-
    visible name we surface in the suggested download filename
    (``<hostname>.kubeconfig``)."""

    configured: bool
    kubeconfig: str | None
    hostname: str


@router.post(
    "/appliances/{appliance_id}/k8s/kubeconfig/reveal",
    response_model=RevealKubeconfigResponse,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Reveal an appliance's k3s admin kubeconfig (superadmin + password)",
)
async def reveal_appliance_kubeconfig(
    appliance_id: uuid.UUID,
    body: RevealKubeconfigRequest,
    current_user: CurrentUser,
    db: DB,
) -> RevealKubeconfigResponse:
    """Return the appliance's stored kubeconfig after password
    re-verification. Same shape as ``POST /admin/agent-keys/reveal``
    and ``POST /settings/snmp/reveal-community``:

    * Superadmin gate
    * Local-auth gate (external-auth users have no password to
      re-confirm)
    * Password re-verification
    * Every denial path emits an audit row + a 100 ms friction sleep

    The kubeconfig's ``server:`` field has already been rewritten to
    the appliance's last-seen IP on the way in (see the heartbeat
    handler). Operators on the appliance's network can use the
    downloaded file directly; operators on a different network may
    need to edit the server line to a reachable address.
    """
    from app.core.crypto import decrypt_str  # noqa: PLC0415
    from app.core.security import verify_password  # noqa: PLC0415

    def _audit_denied(reason: str, *, row: Appliance | None = None) -> None:
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="appliance_kubeconfig_reveal_denied",
                resource_type="appliance",
                resource_id=str(appliance_id),
                resource_display=row.hostname if row else str(appliance_id),
                result="forbidden",
                new_value={"reason": reason},
            )
        )

    if not current_user.is_superadmin:
        _audit_denied("non_superadmin")
        await db.commit()
        await asyncio.sleep(0.1)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only superadmins can reveal an appliance kubeconfig.",
        )
    if current_user.auth_source != "local":
        _audit_denied("external_auth")
        await db.commit()
        await asyncio.sleep(0.1)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Kubeconfig reveal requires a local-auth superadmin "
            f"(your account authenticates via {current_user.auth_source}). "
            "Log in as a local admin to reveal the kubeconfig.",
        )
    if not current_user.hashed_password or not verify_password(
        body.password, current_user.hashed_password
    ):
        _audit_denied("password_mismatch")
        await db.commit()
        await asyncio.sleep(0.1)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password is incorrect.")

    row = await db.get(Appliance, appliance_id)
    if row is None:
        _audit_denied("appliance_not_found")
        await db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.kubeconfig_encrypted is None:
        # Clean "nothing to reveal" — supervisor hasn't shipped a
        # kubeconfig yet (legacy compose / pre-Phase-5 / k3s not
        # started). Surface as a friendly state, not an error.
        return RevealKubeconfigResponse(configured=False, kubeconfig=None, hostname=row.hostname)

    try:
        plaintext = decrypt_str(row.kubeconfig_encrypted)
    except Exception:  # noqa: BLE001
        _audit_denied("decrypt_failed", row=row)
        await db.commit()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Stored kubeconfig could not be decrypted (key mismatch?). "
            "The supervisor will re-ship it on the next heartbeat.",
        ) from None

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance_kubeconfig_revealed",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
        )
    )
    await db.commit()
    logger.info(
        "appliance_kubeconfig_revealed",
        appliance_id=str(row.id),
        hostname=row.hostname,
        user=current_user.username,
    )
    return RevealKubeconfigResponse(configured=True, kubeconfig=plaintext, hostname=row.hostname)


# ── Issue #183 Phase 6 — kubeapi-expose CIDR editor ──


class ApplianceKubeapiCidrsRequest(BaseModel):
    """Operator-supplied list of CIDRs allowed to reach this
    appliance's kubeapi on tcp/6443. Validated as a list of strings;
    empty list = proxy-only (the default, recommended posture)."""

    cidrs: list[str] = Field(default_factory=list, max_length=32)


def _validate_cidr(value: str) -> str:
    """Light CIDR validation — operator may type ``10.0.0.0/8`` or
    a bare host ``192.168.1.50`` (which nftables accepts inline).
    Strips whitespace, refuses anything that doesn't parse as an
    ip_network or ip_address. Returns the normalised string the
    nft renderer will emit verbatim."""
    import ipaddress  # noqa: PLC0415

    text = value.strip()
    if not text:
        raise ValueError("empty CIDR")
    try:
        return str(ipaddress.ip_network(text, strict=False))
    except ValueError:
        # Bare host — nft accepts a single-IP rule the same way.
        try:
            return str(ipaddress.ip_address(text))
        except ValueError as exc:
            raise ValueError(f"{text!r} isn't a valid CIDR or IP address") from exc


@router.put(
    "/appliances/{appliance_id}/kubeapi-cidrs",
    response_model=ApplianceRow,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Set the kubeapi-direct-access CIDR allowlist on an appliance",
)
async def update_appliance_kubeapi_cidrs(
    appliance_id: uuid.UUID,
    body: ApplianceKubeapiCidrsRequest,
    current_user: CurrentUser,
    db: DB,
) -> ApplianceRow:
    """Update the operator-controlled CIDR allowlist for direct
    kubeapi access. The supervisor's firewall renderer reads this
    list on every heartbeat + emits ``ip saddr { ... } tcp dport
    6443 accept`` rules.

    Empty list = proxy-only (the recommended default): kubeapi stays
    on 127.0.0.1 and the only path into it is the supervisor's
    outbound proxy channel (Phase 4). Non-empty list = direct
    access for operators who want sub-millisecond local-network
    ops; the supervisor's mTLS proxy still works alongside.
    """
    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")

    normalised: list[str] = []
    for value in body.cidrs:
        try:
            normalised.append(_validate_cidr(value))
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                str(exc),
            ) from exc

    row.kubeapi_expose_cidrs = normalised
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.kubeapi_cidrs_updated",
            resource_type="appliance",
            resource_id=str(row.id),
            resource_display=row.hostname,
            result="success",
            new_value={"kubeapi_expose_cidrs": normalised},
        )
    )
    await db.commit()
    logger.info(
        "appliance.kubeapi_cidrs_updated",
        appliance_id=str(row.id),
        hostname=row.hostname,
        cidr_count=len(normalised),
        user=current_user.username,
    )
    return _row_to_schema(row)


# ── Issue #183 Phase 8 — pod list + log viewer (via Phase 4 proxy) ──


class K8sPodSummary(BaseModel):
    """Trimmed shape of a kubeapi Pod for the operator's log-picker
    dropdown. We don't surface the full PodSpec — operators only
    need to pick (pod, container) for the log fetch."""

    name: str
    namespace: str
    phase: str
    ready: bool
    containers: list[str]
    labels: dict[str, str]


class K8sPodListResponse(BaseModel):
    pods: list[K8sPodSummary]


@router.get(
    "/appliances/{appliance_id}/k8s/pods",
    response_model=K8sPodListResponse,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="List pods on the appliance's local k3s (via the supervisor proxy)",
)
async def k8s_list_pods(
    appliance_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    namespace: str = "spatium",
) -> K8sPodListResponse:
    """List pods in the named namespace via the kubeapi proxy.

    Default namespace ``spatium`` covers every chart-deployed pod.
    Operators who want kube-system / default visibility pass the
    namespace explicitly — same kubeapi surface as ``kubectl get
    pods``.
    """
    from app.services.appliance import k8s_proxy as _proxy  # noqa: PLC0415

    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state != APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Appliance in state {row.state!r}; kubeapi proxy unavailable.",
        )

    path = f"/api/v1/namespaces/{namespace}/pods"
    try:
        status_code, body = await _proxy.k8s_call(row.id, "GET", path, timeout=15.0)
    except TimeoutError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "Supervisor didn't reply in 15s. Is the appliance heartbeating?",
        ) from exc
    if not 200 <= status_code < 300:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"kubeapi returned {status_code}: {body[:200]!r}",
        )

    try:
        import json  # noqa: PLC0415

        data = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"kubeapi returned non-JSON body: {exc}",
        ) from exc

    pods: list[K8sPodSummary] = []
    for item in data.get("items") or []:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        st = item.get("status") or {}
        container_statuses = st.get("containerStatuses") or []
        ready = bool(container_statuses) and all(cs.get("ready") for cs in container_statuses)
        pods.append(
            K8sPodSummary(
                name=meta.get("name") or "",
                namespace=meta.get("namespace") or namespace,
                phase=st.get("phase") or "Unknown",
                ready=ready,
                containers=[
                    c.get("name") or "" for c in (spec.get("containers") or []) if c.get("name")
                ],
                labels=dict(meta.get("labels") or {}),
            )
        )
    return K8sPodListResponse(pods=pods)


@router.get(
    "/appliances/{appliance_id}/k8s/logs",
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Fetch recent pod logs from the appliance's local k3s",
)
async def k8s_get_pod_logs(
    appliance_id: uuid.UUID,
    pod: str,
    current_user: CurrentUser,
    db: DB,
    namespace: str = "spatium",
    container: str | None = None,
    tail_lines: int = 1000,
):
    """Return the last ``tail_lines`` lines of the named pod's log
    via the kubeapi proxy.

    Snapshot-mode rather than ``--follow``: the Phase 4 proxy is
    request/response. For true ``kubectl logs -f``-style streaming
    the operator can ssh to the appliance + run the kubectl directly
    (kubeconfig revealed via the Fleet UI). A future Phase 8b
    follow-up extends the proxy with a streaming-response channel.

    Returns ``text/plain`` so the Fleet UI's textarea can render it
    verbatim. Up to 1 MiB per call — k3s tail-line cap by default.
    """
    from fastapi.responses import PlainTextResponse  # noqa: PLC0415

    from app.services.appliance import k8s_proxy as _proxy  # noqa: PLC0415

    _require_superadmin(current_user)
    row = await db.get(Appliance, appliance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appliance not found.")
    if row.state != APPLIANCE_STATE_APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Appliance in state {row.state!r}; kubeapi proxy unavailable.",
        )

    # Reject obvious path-injection on operator-supplied strings —
    # the proxy concatenates them into the kubeapi URL.
    if "/" in pod or "/" in namespace or (container and "/" in container):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid identifier")
    tail = max(1, min(10_000, tail_lines))

    path = f"/api/v1/namespaces/{namespace}/pods/{pod}/log?tailLines={tail}"
    if container:
        path += f"&container={container}"

    try:
        status_code, body = await _proxy.k8s_call(
            row.id, "GET", path, accept="text/plain", timeout=20.0
        )
    except TimeoutError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "Supervisor didn't reply in 20s.",
        ) from exc
    if not 200 <= status_code < 300:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"kubeapi returned {status_code}: {body[:200]!r}",
        )
    return PlainTextResponse(body.decode("utf-8", errors="replace"))
