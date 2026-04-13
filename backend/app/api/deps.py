"""Shared FastAPI dependencies injected into route handlers."""

from collections.abc import AsyncGenerator
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db import get_db
from app.models.auth import User

logger = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> User:
    """
    Validate Bearer JWT and return the authenticated User.
    Raises 401 if missing or invalid; 403 if inactive.
    """
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        payload = decode_access_token(credentials.credentials)
        user_id: str = payload["sub"]
    except (JWTError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    from sqlalchemy import select
    from app.models.auth import User as UserModel

    result = await db.execute(select(UserModel).where(UserModel.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled")

    return user


def require_superadmin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Superadmin required")
    return current_user


# Type aliases for injection
CurrentUser = Annotated[User, Depends(get_current_user)]
SuperAdmin = Annotated[User, Depends(require_superadmin)]
DB = Annotated[AsyncSession, Depends(get_db)]
