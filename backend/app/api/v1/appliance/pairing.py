"""Appliance agent pairing — short-lived single-use codes (#169).

Operator-facing problem: registering a new DNS / DHCP agent appliance
with the control plane currently means typing the long opaque
``DNS_AGENT_KEY`` / ``DHCP_AGENT_KEY`` hex string into the installer
wizard on the agent's console. On IPMI / Proxmox / serial consoles
copy-paste is awkward, and the 64-char hex string is easy to typo.

This module adds a thin layer on top: operator clicks "Add appliance"
in the control plane UI → server mints an 8-digit single-use pairing
code with a 15-min expiry → operator notes the 8 digits → agent's
installer asks for the code instead of the hex key → agent POSTs
``/api/v1/appliance/pair`` to swap the code for the real bootstrap
key, which it then uses with the existing bootstrap → JWT flow
unchanged.

Endpoints:

* ``POST /api/v1/appliance/pairing-codes`` — superadmin creates a
  code. Optional ``deployment_kind`` (``dns`` | ``dhcp``),
  ``server_group_id``, ``expires_in_minutes``, ``note``.
* ``GET  /api/v1/appliance/pairing-codes`` — superadmin lists every
  code (active + recent claimed/expired/revoked rows). The cleartext
  code is never re-displayed — only "last 2 digits" + state.
* ``DELETE /api/v1/appliance/pairing-codes/{id}`` — superadmin revokes
  a pending code (no-op on already-used / already-revoked rows).
* ``POST /api/v1/appliance/pair`` — UNAUTHENTICATED. The agent's
  install wizard hits this with ``{code, hostname}``. On success the
  endpoint atomically marks the row claimed and returns the real
  bootstrap key + pre-assigned group (if any). On failure the
  response is a generic 403 so an attacker can't distinguish
  "expired" from "wrong code" from "revoked" via timing or response
  shape. A short constant-time-ish sleep blunts brute-force attempts
  without needing a separate rate-limit table.

Why ``code_hash`` and not the cleartext code: defense-in-depth.
sha256 of an 8-digit decimal is rainbow-tableable in seconds, so this
isn't a real cryptographic gate — but a DB read attacker who's
trying to claim an active code from a backup snapshot has to do at
least a tiny bit of work, and the audit log captures every consume
attempt. Anyone with full DB read already has the bootstrap keys
themselves via ``platform_settings``, so this is hygiene, not security.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.config import settings
from app.core.permissions import require_permission
from app.models.appliance import PairingCode
from app.models.audit import AuditLog
from app.models.dhcp import DHCPServerGroup
from app.models.dns import DNSServerGroup

logger = structlog.get_logger(__name__)

router = APIRouter()


# ── Code generation ────────────────────────────────────────────────


_CODE_LENGTH = 8
_CODE_ALPHABET = "0123456789"
# Bounds on caller-supplied ``expires_in_minutes``.
_MIN_EXPIRY_MINUTES = 5
_MAX_EXPIRY_MINUTES = 60
_DEFAULT_EXPIRY_MINUTES = 15
# Brute-force-friction sleep on every failed consume. Constant time so
# we don't leak distinguishing latency between failure modes.
_CONSUME_FAILURE_DELAY_S = 0.5


def _generate_code() -> str:
    """Cryptographically-secure 8-decimal-digit pairing code."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _hash_code(code: str) -> str:
    """sha256 hex digest. Codes are short + low-entropy by themselves,
    so the hash is defense-in-depth (audit-log distinguishability for a
    DB-snapshot attacker), not a real cryptographic gate.
    """
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _client_ip(request: Request) -> str | None:
    """Real source IP via uvicorn ``--proxy-headers`` (configured in
    the API Dockerfile); falls back to the raw socket address."""
    return request.client.host if request.client else None


# ── Schemas ────────────────────────────────────────────────────────


DeploymentKind = Literal["dns", "dhcp"]


class PairingCodeCreate(BaseModel):
    deployment_kind: DeploymentKind = Field(
        description="Which agent kind this code provisions: 'dns' or 'dhcp'."
    )
    server_group_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional pre-assignment to a DNS or DHCP server group. "
            "Must match the deployment_kind."
        ),
    )
    expires_in_minutes: int = Field(
        default=_DEFAULT_EXPIRY_MINUTES,
        ge=_MIN_EXPIRY_MINUTES,
        le=_MAX_EXPIRY_MINUTES,
        description=(
            f"Code validity window. Default {_DEFAULT_EXPIRY_MINUTES} min; "
            f"range {_MIN_EXPIRY_MINUTES}-{_MAX_EXPIRY_MINUTES}."
        ),
    )
    note: str | None = Field(
        default=None,
        max_length=255,
        description="Free-form operator note, e.g. 'for dns-west-2'.",
    )


class PairingCodeCreated(BaseModel):
    """Response from a successful create — the only response that
    carries the cleartext code. Re-fetching the row later returns the
    redacted shape (``code_last_two`` only)."""

    id: uuid.UUID
    code: str = Field(description="8-digit cleartext code; shown ONCE, never persisted.")
    deployment_kind: DeploymentKind
    server_group_id: uuid.UUID | None
    note: str | None
    expires_at: datetime
    created_at: datetime


class PairingCodeRow(BaseModel):
    """Redacted shape used by the list endpoint. ``code_last_two`` is
    the only fragment of the code surfaced so a glance can correlate
    a code an operator has written down to its row, without re-exposing
    the secret.
    """

    id: uuid.UUID
    code_last_two: str = Field(description="Last two digits, e.g. '47'; for visual correlation.")
    deployment_kind: DeploymentKind
    server_group_id: uuid.UUID | None
    server_group_name: str | None = None
    note: str | None
    state: Literal["pending", "claimed", "expired", "revoked"]
    expires_at: datetime
    used_at: datetime | None
    used_by_ip: str | None
    used_by_hostname: str | None
    revoked_at: datetime | None
    created_at: datetime
    created_by_user_id: uuid.UUID | None


class PairingCodeList(BaseModel):
    codes: list[PairingCodeRow]


class PairConsumeRequest(BaseModel):
    code: str = Field(min_length=_CODE_LENGTH, max_length=_CODE_LENGTH)
    hostname: str | None = Field(
        default=None,
        max_length=255,
        description="Optional hostname the agent reports; captured for audit + UI.",
    )

    @field_validator("code")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("Pairing code must be 8 decimal digits.")
        return v


class PairConsumeResponse(BaseModel):
    """Returned on successful pair. The agent persists ``bootstrap_key``
    to its existing on-disk config (same path it would write a pasted
    key) and continues with the existing bootstrap → JWT flow."""

    bootstrap_key: str
    deployment_kind: DeploymentKind
    server_group_id: uuid.UUID | None


# ── Helpers ────────────────────────────────────────────────────────


def _state_for(
    row: PairingCode, now: datetime
) -> Literal["pending", "claimed", "expired", "revoked"]:
    """Derive the operator-facing state from the row's nullable columns
    + wall clock. Precedence: claimed > revoked > expired > pending.
    """
    if row.used_at is not None:
        return "claimed"
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at <= now:
        return "expired"
    return "pending"


async def _resolve_group_name(db: DB, kind: str, group_id: uuid.UUID | None) -> str | None:
    if group_id is None:
        return None
    if kind == "dns":
        dns_row = await db.get(DNSServerGroup, group_id)
        return dns_row.name if dns_row is not None else None
    if kind == "dhcp":
        dhcp_row = await db.get(DHCPServerGroup, group_id)
        return dhcp_row.name if dhcp_row is not None else None
    return None


def _bootstrap_key_for(kind: str) -> str | None:
    if kind == "dns":
        return settings.dns_agent_key or None
    if kind == "dhcp":
        return settings.dhcp_agent_key or None
    return None


def _require_superadmin(user: CurrentUser) -> None:
    if not user.is_superadmin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Pairing-code management is restricted to superadmins.",
        )


# ── Create ─────────────────────────────────────────────────────────


@router.post(
    "/pairing-codes",
    response_model=PairingCodeCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Mint a new pairing code (superadmin)",
)
async def create_pairing_code(
    body: PairingCodeCreate,
    current_user: CurrentUser,
    db: DB,
) -> PairingCodeCreated:
    _require_superadmin(current_user)

    # Validate the optional pre-assigned group resolves to a real row
    # of the right kind. Reject early with a clear 422 rather than
    # silently issuing a code that can't be redeemed.
    if body.server_group_id is not None:
        group_exists: bool
        if body.deployment_kind == "dns":
            dns_grp = await db.get(DNSServerGroup, body.server_group_id)
            group_exists = dns_grp is not None
        else:
            dhcp_grp = await db.get(DHCPServerGroup, body.server_group_id)
            group_exists = dhcp_grp is not None
        if not group_exists:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"server_group_id does not match any {body.deployment_kind} server group.",
            )

    # Refuse if the corresponding bootstrap key isn't configured —
    # otherwise the code would be unredeemable at consume time, which
    # is worse UX than a clear "configure the env var first" message.
    if not _bootstrap_key_for(body.deployment_kind):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No {body.deployment_kind.upper()}_AGENT_KEY configured on the control plane. "
            "Set it in /etc/spatiumddi/.env and restart the api before issuing codes.",
        )

    code = _generate_code()
    code_hash = _hash_code(code)
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=body.expires_in_minutes)

    # Materialise the row's UUID up-front so the audit row we add in
    # the same flush carries the right resource_id. SQLAlchemy's
    # ``default=uuid.uuid4`` on the mapped_column fires during flush
    # (after this block), which is too late for the audit row's
    # resource_id we're computing right now.
    row_id = uuid.uuid4()
    row = PairingCode(
        id=row_id,
        code_hash=code_hash,
        code_last_two=code[-2:],
        deployment_kind=body.deployment_kind,
        server_group_id=body.server_group_id,
        expires_at=expires_at,
        note=body.note,
        created_by_user_id=current_user.id,
    )
    db.add(row)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.pairing_code_created",
            resource_type="pairing_code",
            resource_id=str(row_id),
            resource_display=f"{body.deployment_kind} pairing code (••{code[-2:]})",
            result="success",
            new_value={
                "deployment_kind": body.deployment_kind,
                "server_group_id": str(body.server_group_id) if body.server_group_id else None,
                "expires_in_minutes": body.expires_in_minutes,
                "note": body.note,
            },
        )
    )
    await db.commit()
    await db.refresh(row)

    logger.info(
        "pairing_code_created",
        id=str(row.id),
        deployment_kind=body.deployment_kind,
        expires_at=expires_at.isoformat(),
        user=current_user.username,
    )

    return PairingCodeCreated(
        id=row.id,
        code=code,
        deployment_kind=body.deployment_kind,
        server_group_id=body.server_group_id,
        note=body.note,
        expires_at=expires_at,
        created_at=row.created_at,
    )


# ── List ───────────────────────────────────────────────────────────


@router.get(
    "/pairing-codes",
    response_model=PairingCodeList,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="List pairing codes (superadmin)",
)
async def list_pairing_codes(
    current_user: CurrentUser,
    db: DB,
    include_terminal: bool = Query(
        default=True,
        description="Include claimed/expired/revoked rows alongside pending.",
    ),
) -> PairingCodeList:
    _require_superadmin(current_user)

    stmt = select(PairingCode).order_by(PairingCode.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    now = datetime.now(UTC)

    out: list[PairingCodeRow] = []
    for row in rows:
        state = _state_for(row, now)
        if not include_terminal and state != "pending":
            continue
        out.append(
            PairingCodeRow(
                id=row.id,
                code_last_two=row.code_last_two,
                deployment_kind=row.deployment_kind,  # type: ignore[arg-type]
                server_group_id=row.server_group_id,
                server_group_name=await _resolve_group_name(
                    db, row.deployment_kind, row.server_group_id
                ),
                note=row.note,
                state=state,
                expires_at=row.expires_at,
                used_at=row.used_at,
                used_by_ip=row.used_by_ip,
                used_by_hostname=row.used_by_hostname,
                revoked_at=row.revoked_at,
                created_at=row.created_at,
                created_by_user_id=row.created_by_user_id,
            )
        )
    return PairingCodeList(codes=out)


# ── Revoke ─────────────────────────────────────────────────────────


@router.delete(
    "/pairing-codes/{code_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("admin", "appliance"))],
    summary="Revoke a pending pairing code (superadmin)",
)
async def revoke_pairing_code(
    code_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
) -> None:
    _require_superadmin(current_user)

    row = await db.get(PairingCode, code_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pairing code not found.")

    # Revoke is idempotent on terminal states — already-claimed and
    # already-revoked rows return 204 with no DB change. ``used_at``
    # always wins over ``revoked_at`` for state derivation.
    if row.used_at is not None or row.revoked_at is not None:
        return None

    now = datetime.now(UTC)
    row.revoked_at = now
    row.revoked_by_user_id = current_user.id
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="appliance.pairing_code_revoked",
            resource_type="pairing_code",
            resource_id=str(row.id),
            resource_display=f"{row.deployment_kind} pairing code",
            result="success",
        )
    )
    await db.commit()
    logger.info(
        "pairing_code_revoked",
        id=str(row.id),
        user=current_user.username,
    )
    return None


# ── Consume (unauthenticated) ──────────────────────────────────────


@router.post(
    "/pair",
    response_model=PairConsumeResponse,
    summary="Exchange a pairing code for the agent bootstrap key (unauthenticated)",
)
async def consume_pairing_code(
    body: PairConsumeRequest,
    request: Request,
    db: DB,
) -> PairConsumeResponse:
    """Agent-side endpoint. No auth — the pairing code IS the auth.

    Failure modes are deliberately collapsed into a single 403 with a
    generic message so an attacker brute-forcing the namespace can't
    distinguish "this code doesn't exist" from "this code expired"
    from "this code is already used". A constant short sleep on
    failure adds friction without needing a separate rate-limit table.
    """
    submitted_hash = _hash_code(body.code)
    client_ip = _client_ip(request)
    now = datetime.now(UTC)

    # Look up by hash. Single-row index hit; collision is statistically
    # impossible (sha256 of distinct 8-digit codes).
    stmt = select(PairingCode).where(PairingCode.code_hash == submitted_hash)
    row = (await db.execute(stmt)).scalar_one_or_none()

    failure_reason: str | None = None
    if row is None:
        failure_reason = "unknown_code"
    elif row.used_at is not None:
        failure_reason = "already_used"
    elif row.revoked_at is not None:
        failure_reason = "revoked"
    elif row.expires_at <= now:
        failure_reason = "expired"

    if failure_reason is not None:
        # Audit the denial — operator-facing signal for "someone tried
        # to redeem a code they shouldn't have". Capture IP + reason
        # but NOT the submitted code (it might be a typo close to a
        # real code).
        db.add(
            AuditLog(
                user_id=None,
                user_display_name="anonymous",
                auth_source="anonymous",
                source_ip=client_ip,
                action="appliance.pairing_code_consume_denied",
                resource_type="pairing_code",
                resource_id=str(row.id) if row is not None else "unknown",
                resource_display=(
                    f"{row.deployment_kind} pairing code"
                    if row is not None
                    else "unknown pairing code"
                ),
                result="forbidden",
                new_value={"reason": failure_reason, "hostname": body.hostname},
            )
        )
        await db.commit()
        logger.warning(
            "pairing_code_consume_denied",
            reason=failure_reason,
            ip=client_ip,
            hostname=body.hostname,
        )
        # Constant delay regardless of which failure path we took, so
        # response-timing doesn't leak distinguishability.
        await asyncio.sleep(_CONSUME_FAILURE_DELAY_S)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Pairing code is invalid, expired, or already used. "
            "Ask your platform admin to generate a new code.",
        )

    # Sanity: the bootstrap key must be configured server-side. We
    # already check this at create time, but the env var could have
    # been blanked between then and now. Treat as 503 — server-side
    # operational issue, not the caller's fault.
    assert row is not None  # narrow for type checker — failure_reason path returned above
    bootstrap_key = _bootstrap_key_for(row.deployment_kind)
    if not bootstrap_key:
        logger.error(
            "pairing_code_consume_no_bootstrap_key",
            id=str(row.id),
            deployment_kind=row.deployment_kind,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Control plane has no bootstrap key configured for this deployment kind. "
            "Contact your platform admin.",
        )

    # Constant-time compare on the hash (already done above via index
    # lookup, but ``hmac.compare_digest`` is the canonical idiom here
    # so future refactors don't regress to plain ``==``).
    if not hmac.compare_digest(row.code_hash, submitted_hash):  # pragma: no cover
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Hash mismatch.")

    row.used_at = now
    row.used_by_ip = client_ip
    row.used_by_hostname = body.hostname

    db.add(
        AuditLog(
            user_id=None,
            user_display_name=body.hostname or "anonymous agent",
            auth_source="pairing_code",
            source_ip=client_ip,
            action="appliance.pairing_code_claimed",
            resource_type="pairing_code",
            resource_id=str(row.id),
            resource_display=f"{row.deployment_kind} pairing code",
            result="success",
            new_value={
                "deployment_kind": row.deployment_kind,
                "server_group_id": str(row.server_group_id) if row.server_group_id else None,
                "hostname": body.hostname,
            },
        )
    )
    await db.commit()
    logger.info(
        "pairing_code_claimed",
        id=str(row.id),
        deployment_kind=row.deployment_kind,
        ip=client_ip,
        hostname=body.hostname,
    )

    return PairConsumeResponse(
        bootstrap_key=bootstrap_key,
        deployment_kind=row.deployment_kind,  # type: ignore[arg-type]
        server_group_id=row.server_group_id,
    )
