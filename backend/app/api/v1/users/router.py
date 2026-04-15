"""User management endpoints (superadmin only)."""

import uuid

import bcrypt
import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DB, SuperAdmin
from app.models.audit import AuditLog
from app.models.auth import User

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

    model_config = {"from_attributes": True}

    @field_validator("id", mode="before")
    @classmethod
    def coerce_id(cls, v: object) -> str:
        return str(v)

    @field_validator("last_login_at", mode="before")
    @classmethod
    def coerce_dt(cls, v: object) -> str | None:
        return v.isoformat() if v is not None else None


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

    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = User(
        username=body.username,
        email=body.email,
        display_name=body.display_name,
        hashed_password=hashed,
        is_superadmin=body.is_superadmin,
        force_password_change=body.force_password_change,
        auth_source="local",
        is_active=True,
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
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.hashed_password = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    user.force_password_change = True
    db.add(
        _audit(current_user, "reset_password", str(user.id), f"Reset password for {user.username}")
    )
    await db.commit()
    logger.info("password_reset", target=user.username, by=current_user.username)


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
