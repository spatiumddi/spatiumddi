"""Shared FastAPI dependencies injected into route handlers."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token, hash_api_token
from app.db import get_db
from app.models.auth import APIToken, User, UserSession
from app.services.api_token_scopes import scope_matches_request

logger = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=False)

# API tokens issued by SpatiumDDI all carry this prefix so the auth
# middleware can distinguish them from JWTs without an extra DB round-trip
# on every request. See ``app.core.security.generate_api_token``.
_API_TOKEN_PREFIX = "sddi_"


async def _resolve_api_token(db: AsyncSession, raw: str, request: Request) -> User:
    """Validate an ``sddi_*`` bearer and return the owning user.

    Raises the same 401/403 pattern as JWT auth so callers can't
    distinguish "no token" from "expired token" from "revoked token".
    Successful lookups also bump ``last_used_at`` so operators have a
    single column they can glance at to see which tokens are live
    vs. dead.

    The ``request`` arg lets us enforce ``token.scopes`` BEFORE the
    RBAC check downstream — see ``app.services.api_token_scopes``. A
    "read-only" token can never reach a write handler, even if the
    owner's RBAC would allow it.
    """
    token_hash = hash_api_token(raw)
    token = (
        await db.execute(select(APIToken).where(APIToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if token is None or not token.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API token",
        )
    now = datetime.now(UTC)
    if token.expires_at is not None and token.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API token has expired",
        )
    # Coarse-grained scope gate. Empty list = no restriction; the
    # vocabulary check happens at create time so we can trust the
    # stored values here.
    scopes = list(token.scopes or [])
    if scopes and not scope_matches_request(scopes, request.method, request.url.path):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token scope insufficient for this request",
        )
    if token.user_id is None:
        # Scope "global" isn't wired through permissions yet — reject
        # until we add a synthetic service-account path. Today's UI
        # only issues user-scoped tokens so this is defensive.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Global-scope API tokens are not yet supported",
        )
    user = (await db.execute(select(User).where(User.id == token.user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled"
        )
    # Fire-and-forget last-used bump. Failure to write this shouldn't
    # 500 the request — we commit on the caller's session so if the
    # caller rolls back, the timestamp rolls with it (acceptable).
    token.last_used_at = now
    return user


async def get_current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> User:
    """
    Validate a Bearer credential and return the authenticated User.

    Accepts either:
      * a JWT access token issued by ``/auth/login`` (user sessions), or
      * an API token issued by ``/api-tokens`` (machine / script access).

    Raises 401 if missing or invalid; 403 if the user is inactive.
    """
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    raw = credentials.credentials
    # Fast path for API tokens — they carry a distinct prefix so we
    # never try to JWT-decode one (which would just 401 on signature
    # mismatch anyway, but this is cleaner error messaging).
    if raw.startswith(_API_TOKEN_PREFIX):
        return await _resolve_api_token(db, raw, request)

    try:
        payload = decode_access_token(raw)
        user_id: str = payload["sub"]
    except (JWTError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    # Issue #72 — session viewer / force-logout. Tokens minted after
    # the session-viewer landing carry a ``jti`` claim that maps to a
    # ``UserSession`` row. We reject if that row is revoked or expired,
    # which is the force-logout effect: the superadmin flips
    # ``revoked``, every in-flight access token using that jti starts
    # 401-ing on the next request. Tokens without a ``jti`` (legacy or
    # in-flight at deploy time) are allowed through — they expire on
    # their own short TTL.
    jti = payload.get("jti")
    if jti is not None:
        session = await db.get(UserSession, jti)
        if session is None or session.revoked or session.expires_at <= datetime.now(UTC):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session revoked or expired",
            )
        # Bump ``last_seen_at`` no more than once per minute per
        # session — gives the admin viewer a recent timestamp without
        # a write on every authenticated request.
        now = datetime.now(UTC)
        if session.last_seen_at is None or (now - session.last_seen_at) > timedelta(seconds=60):
            session.last_seen_at = now
            try:
                await db.commit()
            except Exception:  # noqa: BLE001 — last_seen is best-effort
                await db.rollback()

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled"
        )

    return user


def require_superadmin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """Admit users who are either
    (a) the legacy ``User.is_superadmin=True`` (seeded ``admin`` / anyone
        explicitly flagged), OR
    (b) granted a Wave-C RBAC wildcard permission (`action=*`,
        `resource_type=*`) via a group → role, i.e. the built-in ``Superadmin``
        role or a custom clone of it.

    Without (b), users provisioned via LDAP / OIDC / SAML and mapped to the
    ``Superadmins`` internal group could pass RBAC-gated checks but still get
    403 on ``SuperAdmin``-gated endpoints (users / groups / roles / auth
    providers / settings) — a split-brain between the legacy flag and the
    RBAC model. This unifies them.
    """
    if current_user.is_superadmin:
        return current_user
    # Lazy import: `app.core.permissions` imports ``CurrentUser`` / ``get_db``
    # from this module at top-level, so an eager import here triggers a
    # circular-import crash at uvicorn startup. Local import side-steps it
    # because by the time this function is called the module graph is fully
    # initialised.
    from app.core.permissions import user_has_permission

    if user_has_permission(current_user, "*", "*"):
        return current_user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Superadmin required")


# Type aliases for injection
CurrentUser = Annotated[User, Depends(get_current_user)]
SuperAdmin = Annotated[User, Depends(require_superadmin)]
DB = Annotated[AsyncSession, Depends(get_db)]
