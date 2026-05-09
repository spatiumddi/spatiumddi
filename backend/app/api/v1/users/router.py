"""User management endpoints (superadmin only)."""

import uuid
from datetime import UTC, datetime

import bcrypt
import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select

from app.api.deps import DB, SuperAdmin
from app.core.demo_mode import forbid_in_demo_mode
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.settings import PlatformSettings
from app.services.account_lockout import (
    is_locked as is_user_locked,
)
from app.services.account_lockout import (
    unlock as unlock_user,
)
from app.services.password_policy import (
    PasswordPolicy,
    push_history,
)
from app.services.password_policy import (
    validate as validate_password_policy,
)

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    display_name: str
    is_active: bool
    is_superadmin: bool
    force_password_change: bool
    auth_source: str
    last_login_at: str | None = None
    # Lockout state (issue #71). ``locked`` mirrors the live time
    # check so the UI doesn't have to compare timestamps in JS;
    # ``failed_login_count`` + ``failed_login_locked_until`` are
    # surfaced for triage / admin display.
    failed_login_count: int = 0
    failed_login_locked_until: str | None = None
    locked: bool = False

    model_config = {"from_attributes": True}

    @field_validator("id", mode="before")
    @classmethod
    def coerce_id(cls, v: object) -> str:
        return str(v)

    @field_validator("last_login_at", "failed_login_locked_until", mode="before")
    @classmethod
    def coerce_dt(cls, v: object) -> str | None:
        return v.isoformat() if v is not None else None

    @model_validator(mode="before")
    @classmethod
    def _compute_locked(cls, data: object) -> object:
        # Fold the ORM ``User`` into a dict so we can attach the
        # computed ``locked`` flag without needing a relationship-side
        # property. Mirrors ``account_lockout.is_locked``.
        if isinstance(data, User):
            cols: dict[str, object] = {
                c.name: getattr(data, c.name) for c in data.__table__.columns
            }
            cols["locked"] = is_user_locked(data)
            return cols
        return data


class CreateUserRequest(BaseModel):
    username: str
    email: str
    display_name: str
    password: str
    is_superadmin: bool = False
    force_password_change: bool = True

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("username")
    @classmethod
    def username_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Username cannot be empty")
        return v


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    email: str | None = None
    is_active: bool | None = None
    is_superadmin: bool | None = None
    force_password_change: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


def _audit(actor: User, action: str, resource_id: str, summary: str) -> AuditLog:
    return AuditLog(
        user_id=actor.id,
        user_display_name=actor.display_name,
        action=action,
        resource_type="user",
        resource_id=resource_id,
        resource_display=summary,
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[UserResponse])
async def list_users(current_user: SuperAdmin, db: DB) -> list[User]:
    result = await db.execute(select(User).order_by(User.username))
    return list(result.scalars().all())


async def _enforce_policy(db: DB, password: str) -> tuple[PasswordPolicy, str]:
    """Validate the candidate password against the active policy and
    return ``(policy, hash)``. Raises 400 on violation. Reused across
    the create-user + reset-password paths so the rules can't drift."""
    settings_row = await db.get(PlatformSettings, 1)
    policy = PasswordPolicy.from_row(settings_row)
    result = validate_password_policy(password, policy)
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": "password_policy", "errors": result.errors},
        )
    return policy, bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserRequest, current_user: SuperAdmin, db: DB) -> User:
    # Check uniqueness
    existing = await db.execute(
        select(User).where((User.username == body.username) | (User.email == body.email))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already in use",
        )

    policy, hashed = await _enforce_policy(db, body.password)
    history = push_history(hashed, None, policy.history_count)
    user = User(
        username=body.username,
        email=body.email,
        display_name=body.display_name,
        hashed_password=hashed,
        is_superadmin=body.is_superadmin,
        force_password_change=body.force_password_change,
        auth_source="local",
        is_active=True,
        password_changed_at=datetime.now(UTC),
        password_history_encrypted=history,
    )
    db.add(user)
    await db.flush()
    db.add(_audit(current_user, "create", str(user.id), f"Created user {body.username}"))
    await db.commit()
    await db.refresh(user)
    logger.info("user_created", username=body.username, by=current_user.username)
    return user


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: uuid.UUID, current_user: SuperAdmin, db: DB) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    body: UpdateUserRequest,
    current_user: SuperAdmin,
    db: DB,
) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Prevent removing superadmin from own account
    if user.id == current_user.id and body.is_superadmin is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove your own superadmin status",
        )

    if body.display_name is not None:
        user.display_name = body.display_name
    if body.email is not None:
        user.email = body.email
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_superadmin is not None:
        user.is_superadmin = body.is_superadmin
    if body.force_password_change is not None:
        user.force_password_change = body.force_password_change

    db.add(_audit(current_user, "update", str(user.id), f"Updated user {user.username}"))
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: uuid.UUID,
    body: ResetPasswordRequest,
    current_user: SuperAdmin,
    db: DB,
) -> None:
    forbid_in_demo_mode("Admin password reset is disabled")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Admin reset bypasses history (an admin reset is by definition out
    # of band — the user's prior choices are not in scope) but still
    # honours the complexity rules so an operator can't side-step the
    # policy via the admin path.
    policy, hashed = await _enforce_policy(db, body.new_password)
    user.hashed_password = hashed
    user.force_password_change = True
    user.password_changed_at = datetime.now(UTC)
    user.password_history_encrypted = push_history(
        hashed, user.password_history_encrypted, policy.history_count
    )
    db.add(
        _audit(current_user, "reset_password", str(user.id), f"Reset password for {user.username}")
    )
    await db.commit()
    logger.info("password_reset", target=user.username, by=current_user.username)


@router.post("/{user_id}/unlock", status_code=status.HTTP_204_NO_CONTENT)
async def unlock_account(
    user_id: uuid.UUID,
    current_user: SuperAdmin,
    db: DB,
) -> None:
    """Clear an account's failed-login counter + locked-until (issue
    #71). Idempotent: if the user wasn't locked, returns 204 without
    writing an audit row."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    changed = unlock_user(user)
    if changed:
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="account.unlocked",
                resource_type="user",
                resource_id=str(user.id),
                resource_display=user.username,
                result="success",
            )
        )
    await db.commit()
    logger.info("account_unlocked", target=user.username, by=current_user.username)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: uuid.UUID, current_user: SuperAdmin, db: DB) -> None:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    db.add(_audit(current_user, "delete", str(user.id), f"Deleted user {user.username}"))
    await db.delete(user)
    await db.commit()
    logger.info("user_deleted", username=user.username, by=current_user.username)
