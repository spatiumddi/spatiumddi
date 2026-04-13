"""Authentication endpoints: login, refresh, logout, current user."""

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select, update

from app.api.deps import CurrentUser, DB
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from app.models.audit import AuditLog
from app.models.auth import User, UserSession

logger = structlog.get_logger(__name__)
router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    force_password_change: bool = False


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    display_name: str
    is_superadmin: bool
    force_password_change: bool
    auth_source: str

    model_config = {"from_attributes": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_not_empty(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: DB) -> TokenResponse:
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    if user is None or not user.hashed_password or not verify_password(body.password, user.hashed_password):
        logger.warning("login_failed", username=body.username, source_ip=request.client.host if request.client else None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")

    access_token = create_access_token(str(user.id))
    raw_refresh, refresh_hash = create_refresh_token(str(user.id))

    from datetime import UTC, datetime, timedelta
    from app.config import settings

    session = UserSession(
        user_id=user.id,
        refresh_token_hash=refresh_hash,
        source_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(session)

    audit = AuditLog(
        user_id=user.id,
        user_display_name=user.display_name,
        auth_source=user.auth_source,
        source_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        action="login",
        resource_type="user",
        resource_id=str(user.id),
        resource_display=user.username,
        result="success",
    )
    db.add(audit)
    await db.commit()

    logger.info("login_success", user_id=str(user.id), username=user.username)
    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        force_password_change=user.force_password_change,
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    current_user: CurrentUser,
    db: DB,
) -> None:
    if not current_user.hashed_password or not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")

    await db.execute(
        update(User)
        .where(User.id == current_user.id)
        .values(hashed_password=hash_password(body.new_password), force_password_change=False)
    )

    audit = AuditLog(
        user_id=current_user.id,
        user_display_name=current_user.display_name,
        auth_source=current_user.auth_source,
        action="update",
        resource_type="user",
        resource_id=str(current_user.id),
        resource_display=current_user.username,
        changed_fields=["hashed_password", "force_password_change"],
        result="success",
    )
    db.add(audit)
    await db.commit()
    logger.info("password_changed", user_id=str(current_user.id))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(current_user: CurrentUser, db: DB) -> None:
    await db.execute(
        update(UserSession)
        .where(UserSession.user_id == current_user.id, UserSession.revoked.is_(False))
        .values(revoked=True)
    )
    audit = AuditLog(
        user_id=current_user.id,
        user_display_name=current_user.display_name,
        auth_source=current_user.auth_source,
        action="logout",
        resource_type="user",
        resource_id=str(current_user.id),
        resource_display=current_user.username,
        result="success",
    )
    db.add(audit)
    await db.commit()


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: CurrentUser) -> User:
    return current_user
