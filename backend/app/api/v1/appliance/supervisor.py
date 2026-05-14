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
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func as sa_func
from sqlalchemy import select

from app.api.deps import DB
from app.models.appliance import (
    APPLIANCE_STATE_PENDING_APPROVAL,
    Appliance,
    PairingClaim,
    PairingCode,
)
from app.models.audit import AuditLog
from app.models.settings import PlatformSettings

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

    @field_validator("pairing_code")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("Pairing code must be 8 decimal digits.")
        return v


class SupervisorRegisterResponse(BaseModel):
    """A2 response shape. Wave B1 will extend with ``cert_pem`` /
    ``ca_chain_pem`` fields populated once the appliance is approved.
    """

    appliance_id: uuid.UUID
    state: Literal["pending_approval", "approved"]
    # Echoed back for the supervisor's audit log line, so the log
    # reads "registered as ab-cd-… with fingerprint 1234abcd…" without
    # needing to re-derive the hash on the client side.
    public_key_fingerprint: str


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
        # heartbeat endpoint.
        existing.last_seen_at = now
        existing.last_seen_ip = client_ip
        if body.supervisor_version:
            existing.supervisor_version = body.supervisor_version
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
    appliance_row = Appliance(
        id=appliance_id,
        hostname=body.hostname,
        public_key_der=pubkey_der,
        public_key_fingerprint=pubkey_fingerprint,
        supervisor_version=body.supervisor_version,
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
    )
