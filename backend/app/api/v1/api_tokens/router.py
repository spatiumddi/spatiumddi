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
from app.core.security import generate_api_token
from app.models.audit import AuditLog
from app.models.auth import APIToken

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

    @field_validator("expires_in_days")
    @classmethod
    def _either_or(cls, v: int | None, info: object) -> int | None:
        # Pydantic v2 passes `info` with `.data` dict of already-validated
        # fields. If both expires_at and expires_in_days are set we
        # prefer expires_at (more precise) and drop the days field.
        return v


class ApiTokenResponse(BaseModel):
    """Safe-to-list representation — no hash, no raw token."""

    id: uuid.UUID
    name: str
    description: str
    prefix: str
    scope: str
    user_id: uuid.UUID | None
    expires_at: datetime | None
    last_used_at: datetime | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


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


# ── Helpers ────────────────────────────────────────────────────────────────


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
    if not current_user.is_superadmin:
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
    token = APIToken(
        name=body.name,
        description=body.description,
        token_hash=token_hash,
        prefix=display_prefix,
        scope="user",
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
    if not current_user.is_superadmin and token.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    return token
