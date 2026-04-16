"""Authentication endpoints: login, refresh, logout, current user,
plus OIDC redirect flow (authorize + callback + provider listing)."""

import asyncio
import secrets as py_secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Callable
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from jose import JWTError
from jose import jwt as jose_jwt
from pydantic import BaseModel, field_validator
from sqlalchemy import select, update

from app.api.deps import DB, CurrentUser
from app.config import settings
from app.core.auth.ldap import LDAPServiceError, authenticate_ldap
from app.core.auth.oidc import OIDCConfig, OIDCServiceError
from app.core.auth.oidc import build_authorize_url as oidc_authorize_url
from app.core.auth.oidc import exchange_code as oidc_exchange_code
from app.core.auth.radius import RADIUSServiceError, authenticate_radius
from app.core.auth.saml import (
    SAMLConfig,
    SAMLServiceError,
    build_authorize_url as saml_authorize_url,
    consume_assertion as saml_consume_assertion,
    sp_metadata_xml,
)
from app.core.auth.tacacs import TACACSServiceError, authenticate_tacacs
from app.core.auth.user_sync import (
    ExternalAuthResult,
    ExternalSyncRejected,
    sync_external_user,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.models.audit import AuditLog
from app.models.auth import User, UserSession
from app.models.auth_provider import PASSWORD_PROVIDER_TYPES, AuthProvider
from app.models.settings import PlatformSettings

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
    id: uuid.UUID
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


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _issue_tokens(
    db: DB, request: Request, user: User, auth_source: str
) -> TokenResponse:
    """Issue access + refresh tokens, create session, write success audit."""
    access_token = create_access_token(str(user.id))
    raw_refresh, refresh_hash = create_refresh_token(str(user.id))

    db.add(
        UserSession(
            user_id=user.id,
            refresh_token_hash=refresh_hash,
            source_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            user_display_name=user.display_name,
            auth_source=auth_source,
            source_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            action="login",
            resource_type="user",
            resource_id=str(user.id),
            resource_display=user.username,
            result="success",
        )
    )
    user.last_login_at = datetime.now(UTC)
    user.last_login_ip = _client_ip(request)
    await db.commit()

    logger.info(
        "login_success",
        user_id=str(user.id),
        username=user.username,
        auth_source=auth_source,
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        force_password_change=user.force_password_change,
    )


async def _audit_login_failure(
    db: DB,
    request: Request,
    username: str,
    reason: str,
    auth_source: str = "local",
    user: User | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            user_display_name=user.display_name if user else (username or "<unknown>"),
            auth_source=auth_source,
            source_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            action="login",
            resource_type="user",
            resource_id=str(user.id) if user else "",
            resource_display=username or "<unknown>",
            result="failure",
            new_value={"reason": reason},
        )
    )
    await db.commit()
    logger.warning(
        "login_failed",
        username=username,
        reason=reason,
        auth_source=auth_source,
        source_ip=_client_ip(request),
    )


# Password-grant dispatch table. Each entry is the sync authenticate function
# (invoked on a worker thread) plus the provider-type-specific ServiceError
# it may raise. All three functions share the shape
#   (provider, username, password) -> ExternalAuthResult | None
_PasswordAuthFn = Callable[
    [AuthProvider, str, str], ExternalAuthResult | None
]
_PASSWORD_AUTH_DISPATCH: dict[str, tuple[_PasswordAuthFn, type[Exception]]] = {
    "ldap": (authenticate_ldap, LDAPServiceError),
    "radius": (authenticate_radius, RADIUSServiceError),
    "tacacs": (authenticate_tacacs, TACACSServiceError),
}


async def _try_external_password_login(
    db: DB, request: Request, username: str, password: str
) -> TokenResponse | None:
    """Iterate every enabled password-flow provider (LDAP / RADIUS / TACACS+)
    by priority. First success wins. Each provider runs in a worker thread
    with a 20s timeout.

    Returns a TokenResponse on success, None if all providers rejected or
    errored (caller is responsible for emitting the final 401 + audit).
    """
    res = await db.execute(
        select(AuthProvider)
        .where(
            AuthProvider.type.in_(PASSWORD_PROVIDER_TYPES),
            AuthProvider.is_enabled.is_(True),
        )
        .order_by(AuthProvider.priority, AuthProvider.name)
    )
    providers = list(res.unique().scalars().all())
    if not providers:
        return None

    for provider in providers:
        dispatch = _PASSWORD_AUTH_DISPATCH.get(provider.type)
        if dispatch is None:
            # Defensive: PROVIDER_TYPES could include a value not wired here yet.
            logger.warning(
                "password_auth_unknown_type",
                provider=provider.name,
                type=provider.type,
            )
            continue
        authenticate_fn, service_exc = dispatch

        try:
            result: ExternalAuthResult | None = await asyncio.wait_for(
                asyncio.to_thread(authenticate_fn, provider, username, password),
                timeout=20,
            )
        except service_exc as exc:
            # Misconfigured / unreachable — log + continue to next provider.
            logger.warning(
                f"{provider.type}_service_error",
                provider=provider.name,
                error=str(exc),
            )
            db.add(
                AuditLog(
                    user_display_name=username,
                    auth_source=provider.name,
                    source_ip=_client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                    action="login",
                    resource_type="auth_provider",
                    resource_id=str(provider.id),
                    resource_display=provider.name,
                    result="error",
                    new_value={"reason": "service_error", "detail": str(exc)[:500]},
                )
            )
            await db.commit()
            continue
        except asyncio.TimeoutError:
            logger.warning(f"{provider.type}_timeout", provider=provider.name)
            db.add(
                AuditLog(
                    user_display_name=username,
                    auth_source=provider.name,
                    source_ip=_client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                    action="login",
                    resource_type="auth_provider",
                    resource_id=str(provider.id),
                    resource_display=provider.name,
                    result="error",
                    new_value={"reason": "timeout"},
                )
            )
            await db.commit()
            continue

        if result is None:
            # Credentials rejected by this provider — try the next one.
            continue

        # Successful external auth. Reconcile into local User table.
        try:
            user = await sync_external_user(db, provider, result)
        except ExternalSyncRejected as exc:
            await _audit_login_failure(
                db,
                request,
                username,
                reason=exc.reason,
                auth_source=provider.name,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            ) from exc

        return await _issue_tokens(db, request, user, auth_source=provider.name)

    return None


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: DB) -> TokenResponse:
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    # ── Local-first ─────────────────────────────────────────────────────────
    if user is not None and user.auth_source == "local":
        if not user.hashed_password or not verify_password(
            body.password, user.hashed_password
        ):
            await _audit_login_failure(
                db, request, body.username, reason="bad_password", user=user
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        if not user.is_active:
            await _audit_login_failure(
                db, request, body.username, reason="account_disabled", user=user
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled"
            )
        return await _issue_tokens(db, request, user, auth_source="local")

    # ── External provider fallthrough (LDAP / RADIUS / TACACS+) ─────────────
    external_response = await _try_external_password_login(
        db, request, body.username, body.password
    )
    if external_response is not None:
        return external_response

    # Existing-but-external user with no matching provider → fall through to
    # the generic 401 so we don't leak account-existence information.
    await _audit_login_failure(
        db,
        request,
        body.username,
        reason="no_match",
        user=user,
        auth_source=user.auth_source if user else "local",
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
    )


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token_endpoint(body: RefreshRequest, db: DB) -> TokenResponse:
    """Exchange a valid refresh token for a new access token (with token rotation)."""
    token_hash = hash_refresh_token(body.refresh_token)

    result = await db.execute(
        select(UserSession)
        .where(UserSession.refresh_token_hash == token_hash)
        .where(UserSession.revoked.is_(False))
        .where(UserSession.expires_at > datetime.now(UTC))
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        )

    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive"
        )

    # Rotate: revoke old session, issue new tokens
    session.revoked = True
    access_token = create_access_token(str(user.id))
    raw_refresh, refresh_hash = create_refresh_token(str(user.id))

    new_session = UserSession(
        user_id=user.id,
        refresh_token_hash=refresh_hash,
        source_ip=session.source_ip,
        user_agent=session.user_agent,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(new_session)
    await db.commit()

    logger.info("token_refreshed", user_id=str(user.id))
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
    if not current_user.hashed_password or not verify_password(
        body.current_password, current_user.hashed_password
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect"
        )

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


# ── OIDC / SAML redirect flow ────────────────────────────────────────────────


class PublicProviderInfo(BaseModel):
    id: uuid.UUID
    name: str
    type: str


@router.get("/providers", response_model=list[PublicProviderInfo])
async def list_public_providers(db: DB) -> list[PublicProviderInfo]:
    """Public (unauthenticated) list of enabled OIDC/SAML providers so the
    login page can render "Sign in with …" buttons. LDAP is excluded — those
    providers authenticate via the ordinary username+password form."""
    res = await db.execute(
        select(AuthProvider)
        .where(AuthProvider.is_enabled.is_(True))
        .where(AuthProvider.type.in_(["oidc", "saml"]))
        .order_by(AuthProvider.priority, AuthProvider.name)
    )
    return [
        PublicProviderInfo(id=p.id, name=p.name, type=p.type)
        for p in res.unique().scalars().all()
    ]


_OIDC_FLOW_COOKIE = "oidc_flow"
_OIDC_FLOW_TTL = 300  # 5 minutes
_SAML_FLOW_COOKIE = "saml_flow"
_SAML_FLOW_TTL = 300
_LOGIN_CALLBACK_PATH = "/login/callback"
_LOGIN_ERROR_PATH = "/login"


async def _app_base_url(db: DB, request: Request) -> str:
    """Admin-configured external URL (preferred) or the incoming request base."""
    row = await db.get(PlatformSettings, 1)
    configured = (row.app_base_url if row else "").rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _oidc_callback_url(base: str, provider_id: uuid.UUID) -> str:
    return f"{base}/api/v1/auth/{provider_id}/callback"


def _sign_flow_token(payload: dict) -> str:
    return jose_jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _verify_flow_token(token: str) -> dict:
    try:
        return jose_jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except JWTError as exc:
        raise ValueError("invalid flow token") from exc


def _login_error_redirect(reason: str) -> RedirectResponse:
    url = f"{_LOGIN_ERROR_PATH}?{urlencode({'error': reason})}"
    return RedirectResponse(url, status_code=302)


@router.get("/{provider_id}/authorize")
async def authorize(
    provider_id: uuid.UUID, request: Request, db: DB
) -> RedirectResponse:
    provider = await db.get(AuthProvider, provider_id)
    if provider is None or not provider.is_enabled:
        raise HTTPException(status_code=404, detail="Provider not found")
    if provider.type == "oidc":
        return await _oidc_start(provider, request, db)
    if provider.type == "saml":
        return await _saml_start(provider, request, db)
    raise HTTPException(
        status_code=400,
        detail=f"Provider type {provider.type!r} does not use the redirect flow",
    )


async def _oidc_start(
    provider: AuthProvider, request: Request, db: DB
) -> RedirectResponse:
    try:
        cfg = OIDCConfig.from_provider(provider)
    except OIDCServiceError as exc:
        logger.warning("oidc_authorize_config_error", provider=provider.name, error=str(exc))
        return _login_error_redirect("oidc_misconfigured")

    state = py_secrets.token_urlsafe(32)
    nonce = py_secrets.token_urlsafe(32)
    base = await _app_base_url(db, request)
    redirect_uri = _oidc_callback_url(base, provider.id)

    try:
        authorize_url = await oidc_authorize_url(
            cfg, str(provider.id), state, nonce, redirect_uri
        )
    except OIDCServiceError as exc:
        logger.warning("oidc_authorize_build_error", provider=provider.name, error=str(exc))
        return _login_error_redirect("oidc_discovery_failed")

    flow_token = _sign_flow_token(
        {
            "provider_id": str(provider.id),
            "state": state,
            "nonce": nonce,
            "exp": int(datetime.now(UTC).timestamp()) + _OIDC_FLOW_TTL,
        }
    )
    response = RedirectResponse(authorize_url, status_code=302)
    response.set_cookie(
        _OIDC_FLOW_COOKIE,
        flow_token,
        max_age=_OIDC_FLOW_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/api/v1/auth/",
    )
    return response


async def _saml_start(
    provider: AuthProvider, request: Request, db: DB
) -> RedirectResponse:
    base = await _app_base_url(db, request)
    try:
        cfg = SAMLConfig.from_provider(provider, base)
    except SAMLServiceError as exc:
        logger.warning("saml_authorize_config_error", provider=provider.name, error=str(exc))
        return _login_error_redirect("saml_misconfigured")

    relay_state = py_secrets.token_urlsafe(32)
    try:
        authorize_url = saml_authorize_url(cfg, base, relay_state)
    except Exception as exc:  # noqa: BLE001 — python3-saml surfaces plain Exception
        logger.warning("saml_authorize_build_error", provider=provider.name, error=str(exc))
        return _login_error_redirect("saml_build_failed")

    flow_token = _sign_flow_token(
        {
            "provider_id": str(provider.id),
            "relay_state": relay_state,
            "exp": int(datetime.now(UTC).timestamp()) + _SAML_FLOW_TTL,
        }
    )
    response = RedirectResponse(authorize_url, status_code=302)
    response.set_cookie(
        _SAML_FLOW_COOKIE,
        flow_token,
        max_age=_SAML_FLOW_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/api/v1/auth/",
    )
    return response


@router.get("/{provider_id}/callback")
async def oidc_callback(
    provider_id: uuid.UUID,
    request: Request,
    db: DB,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    provider = await db.get(AuthProvider, provider_id)
    if provider is None or provider.type != "oidc":
        raise HTTPException(status_code=404, detail="Provider not found")

    flow_cookie = request.cookies.get(_OIDC_FLOW_COOKIE)
    if not flow_cookie:
        return _login_error_redirect("oidc_state_missing")
    try:
        flow = _verify_flow_token(flow_cookie)
    except ValueError:
        return _login_error_redirect("oidc_state_invalid")
    if flow.get("provider_id") != str(provider_id):
        return _login_error_redirect("oidc_state_mismatch")
    if state != flow.get("state"):
        return _login_error_redirect("oidc_state_mismatch")
    if error:
        logger.warning("oidc_idp_error", provider=provider.name, error=error)
        return _login_error_redirect(f"oidc_idp_{error}")
    if not code:
        return _login_error_redirect("oidc_no_code")

    try:
        cfg = OIDCConfig.from_provider(provider)
        base = await _app_base_url(db, request)
        redirect_uri = _oidc_callback_url(base, provider.id)
        result = await oidc_exchange_code(
            cfg, str(provider.id), code, redirect_uri, flow["nonce"]
        )
    except OIDCServiceError as exc:
        logger.warning("oidc_exchange_failed", provider=provider.name, error=str(exc))
        db.add(
            AuditLog(
                user_display_name="<unknown>",
                auth_source=provider.name,
                source_ip=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                action="login",
                resource_type="auth_provider",
                resource_id=str(provider.id),
                resource_display=provider.name,
                result="error",
                new_value={"reason": "oidc_exchange", "detail": str(exc)[:500]},
            )
        )
        await db.commit()
        return _login_error_redirect("oidc_exchange_failed")

    try:
        user = await sync_external_user(db, provider, result)
    except ExternalSyncRejected as exc:
        await _audit_login_failure(
            db,
            request,
            result.username or result.external_id,
            reason=exc.reason,
            auth_source=provider.name,
        )
        return _login_error_redirect(f"oidc_rejected_{exc.reason}")

    tokens = await _issue_tokens(db, request, user, auth_source=provider.name)

    frag = urlencode(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "force_password_change": str(tokens.force_password_change).lower(),
        }
    )
    response = RedirectResponse(f"{_LOGIN_CALLBACK_PATH}#{frag}", status_code=302)
    response.delete_cookie(_OIDC_FLOW_COOKIE, path="/api/v1/auth/")
    return response


@router.post("/{provider_id}/callback")
async def saml_callback(
    provider_id: uuid.UUID,
    request: Request,
    db: DB,
    SAMLResponse: str = Form(...),
    RelayState: str | None = Form(default=None),
) -> RedirectResponse:
    provider = await db.get(AuthProvider, provider_id)
    if provider is None or provider.type != "saml":
        raise HTTPException(status_code=404, detail="Provider not found")

    flow_cookie = request.cookies.get(_SAML_FLOW_COOKIE)
    if not flow_cookie:
        return _login_error_redirect("saml_state_missing")
    try:
        flow = _verify_flow_token(flow_cookie)
    except ValueError:
        return _login_error_redirect("saml_state_invalid")
    if flow.get("provider_id") != str(provider_id):
        return _login_error_redirect("saml_state_mismatch")
    if RelayState != flow.get("relay_state"):
        return _login_error_redirect("saml_state_mismatch")

    base = await _app_base_url(db, request)
    try:
        cfg = SAMLConfig.from_provider(provider, base)
        consumed = saml_consume_assertion(
            cfg, base, {"SAMLResponse": SAMLResponse, "RelayState": RelayState}
        )
    except SAMLServiceError as exc:
        logger.warning("saml_assertion_rejected", provider=provider.name, error=str(exc))
        db.add(
            AuditLog(
                user_display_name="<unknown>",
                auth_source=provider.name,
                source_ip=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                action="login",
                resource_type="auth_provider",
                resource_id=str(provider.id),
                resource_display=provider.name,
                result="error",
                new_value={"reason": "saml_rejected", "detail": str(exc)[:500]},
            )
        )
        await db.commit()
        return _login_error_redirect("saml_assertion_rejected")

    try:
        user = await sync_external_user(db, provider, consumed.result)
    except ExternalSyncRejected as exc:
        await _audit_login_failure(
            db,
            request,
            consumed.result.username or consumed.result.external_id,
            reason=exc.reason,
            auth_source=provider.name,
        )
        return _login_error_redirect(f"saml_rejected_{exc.reason}")

    tokens = await _issue_tokens(db, request, user, auth_source=provider.name)
    frag = urlencode(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "force_password_change": str(tokens.force_password_change).lower(),
        }
    )
    response = RedirectResponse(f"{_LOGIN_CALLBACK_PATH}#{frag}", status_code=302)
    response.delete_cookie(_SAML_FLOW_COOKIE, path="/api/v1/auth/")
    return response


@router.get("/{provider_id}/metadata")
async def saml_metadata(
    provider_id: uuid.UUID, request: Request, db: DB
) -> Response:
    """Expose the SP metadata XML so admins can register SpatiumDDI with
    their IdP. Superadmin gate is not required: metadata is not sensitive and
    many IdPs fetch it unauthenticated."""
    provider = await db.get(AuthProvider, provider_id)
    if provider is None or provider.type != "saml":
        raise HTTPException(status_code=404, detail="Provider not found")

    base = await _app_base_url(db, request)
    try:
        cfg = SAMLConfig.from_provider(provider, base)
        xml = sp_metadata_xml(cfg)
    except SAMLServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=xml, media_type="application/xml")
