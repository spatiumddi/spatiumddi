"""API token CRUD endpoints.

Closes out the API-token Phase 1 item. The ``APIToken`` model, the
``generate_api_token`` / ``hash_api_token`` helpers, and the
`get_current_user` middleware branch that consumes these tokens all
live elsewhere — this module is the HTTP surface.

**Scope today.** Only user-scoped tokens. A token inherits the
permissions of its owning ``User`` via the normal RBAC path — we
don't expose ``allowed_paths`` or the per-token ``permissions``
overrides on create / update yet. Both columns exist on the model so
we can add them later without a migration.

**Security shape.**
- Raw token is shown in the ``POST`` response body once. Never
  stored. Never logged.
- The DB stores only ``token_hash`` (sha256) and a short ``prefix``
  for identification in the UI.
- ``last_used_at`` is bumped by the auth middleware on every
  successful call so operators can tell live tokens from dead ones.
- Revocation is soft (``is_active=False``). Hard delete is allowed
  too — both just reject future calls with 401.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select

from app.api.deps import DB, CurrentUser
from app.core.permissions import is_effective_superadmin, user_has_permission
from app.core.security import generate_api_token
from app.models.audit import AuditLog
from app.models.auth import APIToken
from app.models.dns import DNSZone
from app.models.ipam import Subnet
from app.services.api_token_scopes import validate_resource_grant_shape, validate_scopes

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────


class ApiTokenCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = Field("", max_length=1000)
    # Expiry: either an ISO-8601 timestamp (for precise control from
    # automation) or ``expires_in_days`` (the easier UI affordance).
    # ``None`` on both = never expires (discouraged in the UI copy but
    # allowed because some use cases genuinely need it).
    expires_at: datetime | None = None
    expires_in_days: int | None = Field(None, ge=1, le=3650)
    # Coarse-grained scope vocabulary — see issue #74 +
    # ``app.services.api_token_scopes``. Empty = no restriction.
    scopes: list[str] = Field(default_factory=list)
    # Per-token resource binding (issue #374). Each entry is
    # ``{action, resource_type, resource_id}``; resource_type ∈ {subnet,
    # dns_zone}. Empty = no resource restriction. Validated against the
    # issuing user's RBAC + resource existence in the create handler.
    resource_grants: list[dict] = Field(default_factory=list)

    @field_validator("resource_grants")
    @classmethod
    def _validate_grant_shape(cls, v: list[dict]) -> list[dict]:
        return validate_resource_grant_shape(v)

    @field_validator("expires_in_days")
    @classmethod
    def _either_or(cls, v: int | None, info: object) -> int | None:
        # Pydantic v2 passes `info` with `.data` dict of already-validated
        # fields. If both expires_at and expires_in_days are set we
        # prefer expires_at (more precise) and drop the days field.
        return v

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, v: list[str]) -> list[str]:
        return validate_scopes(v)


class ApiTokenResponse(BaseModel):
    """Safe-to-list representation — no hash, no raw token."""

    id: uuid.UUID
    name: str
    description: str
    prefix: str
    scope: str
    scopes: list[str] = Field(default_factory=list)
    resource_grants: list[dict] = Field(default_factory=list)
    user_id: uuid.UUID | None
    expires_at: datetime | None
    last_used_at: datetime | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("resource_grants", mode="before")
    @classmethod
    def _grants_default(cls, v: object) -> list[dict]:
        # The column is nullable; surface NULL as an empty list on the wire.
        return v if isinstance(v, list) else []


class ApiTokenCreateResponse(ApiTokenResponse):
    """Create response — contains the raw token **once**."""

    token: str = Field(
        ...,
        description=(
            "The raw token string. Shown exactly once — the caller MUST "
            "record it now; there is no way to retrieve it again."
        ),
    )


class ApiTokenUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=1000)
    is_active: bool | None = None
    # Operators can rotate the scope list on an existing token —
    # tighten ("oops, this token shouldn't be writing DNS") without
    # rotating the secret. Empty list explicitly clears restriction.
    scopes: list[str] | None = None

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return validate_scopes(v)


# ── Helpers ────────────────────────────────────────────────────────────────


_GRANT_RESOURCE_MODELS = {"subnet": Subnet, "dns_zone": DNSZone}


async def _validate_resource_grants(db: DB, current_user: CurrentUser, grants: list[dict]) -> None:
    """Issue #374 create-time checks beyond the shape validator: every grant's
    ``resource_id`` must exist, and the issuing user must already hold the
    grant (you cannot mint a token more powerful than yourself). Raises 422 /
    403 respectively.
    """
    import uuid as _uuid  # noqa: PLC0415

    for g in grants:
        rtype = g["resource_type"]
        rid = g["resource_id"]
        model = _GRANT_RESOURCE_MODELS[rtype]
        try:
            obj = await db.get(model, _uuid.UUID(str(rid)))
        except (ValueError, AttributeError):
            obj = None
        if obj is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{rtype} {rid} not found",
            )
        if not user_has_permission(current_user, g["action"], rtype, rid):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"You don't hold '{g['action']}' on {rtype} {rid}; a token "
                    "cannot grant more than its creator."
                ),
            )


def _resolve_expiry(body: ApiTokenCreate) -> datetime | None:
    if body.expires_at is not None:
        # Normalise naive datetimes to UTC so DB comparisons don't
        # drift by the server's local offset.
        if body.expires_at.tzinfo is None:
            return body.expires_at.replace(tzinfo=UTC)
        return body.expires_at
    if body.expires_in_days is not None:
        return datetime.now(UTC) + timedelta(days=body.expires_in_days)
    return None


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("", response_model=list[ApiTokenResponse])
async def list_tokens(db: DB, current_user: CurrentUser) -> list[APIToken]:
    """List tokens owned by the caller. Superadmin sees everyone's."""
    stmt = select(APIToken).order_by(APIToken.created_at.desc())
    if not is_effective_superadmin(current_user):
        stmt = stmt.where(APIToken.user_id == current_user.id)
    return list((await db.execute(stmt)).scalars().all())


@router.post("", response_model=ApiTokenCreateResponse, status_code=201)
async def create_token(
    body: ApiTokenCreate,
    db: DB,
    current_user: CurrentUser,
) -> dict:
    raw, prefix_, token_hash = generate_api_token()
    # ``prefix`` on the model is 10 chars — ``sddi_`` is 5, so we record
    # ``sddi_`` + the first 5 chars of the random body, giving operators a
    # recognisable "sddi_AbCdE" label in the UI without leaking enough
    # entropy to be useful to an attacker.
    display_prefix = raw[:10]
    expires_at = _resolve_expiry(body)
    # Resource grants (#374): existence + "can't exceed yourself" checks.
    await _validate_resource_grants(db, current_user, body.resource_grants)
    token = APIToken(
        name=body.name,
        description=body.description,
        token_hash=token_hash,
        prefix=display_prefix,
        scope="user",
        scopes=body.scopes,
        resource_grants=body.resource_grants or None,
        user_id=current_user.id,
        created_by_user_id=current_user.id,
        expires_at=expires_at,
        is_active=True,
    )
    db.add(token)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="create",
            resource_type="api_token",
            resource_id=str(token.id) if token.id else "?",
            resource_display=body.name,
            result="success",
            new_value={
                "name": body.name,
                "prefix": display_prefix,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "scopes": body.scopes,
                "resource_grants": body.resource_grants,
            },
        )
    )
    await db.commit()
    await db.refresh(token)
    return {
        "id": token.id,
        "name": token.name,
        "description": token.description,
        "prefix": token.prefix,
        "scope": token.scope,
        "scopes": token.scopes,
        "resource_grants": token.resource_grants or [],
        "user_id": token.user_id,
        "expires_at": token.expires_at,
        "last_used_at": token.last_used_at,
        "is_active": token.is_active,
        "created_at": token.created_at,
        "token": raw,
    }


@router.get("/{token_id}", response_model=ApiTokenResponse)
async def get_token(
    token_id: uuid.UUID,
    db: DB,
    current_user: CurrentUser,
) -> APIToken:
    token = await _require_owned(token_id, db, current_user)
    return token


@router.patch("/{token_id}", response_model=ApiTokenResponse)
async def update_token(
    token_id: uuid.UUID,
    body: ApiTokenUpdate,
    db: DB,
    current_user: CurrentUser,
) -> APIToken:
    token = await _require_owned(token_id, db, current_user)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(token, k, v)
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="update",
            resource_type="api_token",
            resource_id=str(token.id),
            resource_display=token.name,
            result="success",
            changed_fields=list(changes.keys()),
            new_value=changes,
        )
    )
    await db.commit()
    await db.refresh(token)
    return token


@router.delete("/{token_id}", status_code=204)
async def delete_token(
    token_id: uuid.UUID,
    db: DB,
    current_user: CurrentUser,
) -> None:
    token = await _require_owned(token_id, db, current_user)
    name = token.name
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="delete",
            resource_type="api_token",
            resource_id=str(token.id),
            resource_display=name,
            result="success",
        )
    )
    await db.execute(delete(APIToken).where(APIToken.id == token_id))
    await db.commit()


async def _require_owned(token_id: uuid.UUID, db: DB, current_user: CurrentUser) -> APIToken:
    token = await db.get(APIToken, token_id)
    if token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    # Non-superadmins can only see their own tokens. Raising 404 here
    # rather than 403 so we don't leak "this token exists, just not
    # yours".
    if not is_effective_superadmin(current_user) and token.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    return token
