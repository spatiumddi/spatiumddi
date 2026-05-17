"""Group/role-based permission checks.

The shape of a permission stored in `Role.permissions` (JSONB) is::

    {"action": "<action>", "resource_type": "<resource_type>", "resource_id": "<uuid?>"}

- ``action``: ``read``, ``write``, ``delete``, ``admin`` (``admin`` implies
  the other three on that resource_type), or the wildcard ``"*"``.
- ``resource_type``: one of the strings enumerated in ``docs/PERMISSIONS.md``
  (e.g. ``ip_space``, ``dns_zone``), or the wildcard ``"*"``.
- ``resource_id``: optional. When absent/None the permission applies to every
  instance of ``resource_type``; when set it scopes to that single UUID.

Superadmin (`User.is_superadmin=True`) short-circuits every check and is never
denied. This is enforced both here and in the FastAPI dependency factories.

Two entry points are exported:

* ``user_has_permission`` — pure helper, safe to call from inside a handler
  after a DB lookup (fine-grained, per-row gate).
* ``require_permission`` — dependency factory for FastAPI. Pass it into
  ``Depends(...)`` / ``dependencies=[Depends(...)]`` at route or router scope.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_db
from app.models.audit import AuditLog
from app.models.auth import User

logger = structlog.get_logger(__name__)

# Actions that are implied by the "admin" action.
_ADMIN_IMPLIES = frozenset({"read", "write", "delete", "admin"})

# Resource type gate for VRF CRUD (issue #86). The "manage_vrfs"
# label in design docs corresponds to ``admin`` on this resource_type
# in the underlying RBAC grammar — the API uses
# ``require_resource_permission("vrf")``. The Network Editor builtin
# role grants ``admin`` on this type; superadmin always bypasses.
RESOURCE_TYPE_VRF = "vrf"

# Appliance management surface (issue #134, Phase 4). Every
# /api/v1/appliance/* router gates on ``read`` or ``admin`` against
# this resource_type. The "Appliance Operator" builtin role grants
# ``admin``; the read-only "Appliance Viewer" role can be added later
# if a NOC team wants visibility without lifecycle control.
#
# Note: the gate is independent from ``settings.appliance_mode``.
# appliance_mode is a deployment-time flag (is the API running on
# the SpatiumDDI OS appliance?); the permission is an authorization
# flag (does THIS user get to drive it?). The router stays mounted
# either way; on a non-appliance deploy every endpoint will 404 or
# return empty data because the underlying OS surfaces aren't there.
RESOURCE_TYPE_APPLIANCE = "appliance"


def _action_matches(granted: str, requested: str) -> bool:
    """Return True if a granted `action` string covers the requested action."""
    if granted == "*" or granted == requested:
        return True
    if granted == "admin" and requested in _ADMIN_IMPLIES:
        return True
    return False


def _resource_type_matches(granted: str, requested: str) -> bool:
    return granted == "*" or granted == requested


def _resource_id_matches(granted: str | None, requested: str | None) -> bool:
    """Granted=None means any instance. Otherwise must be equal."""
    if granted is None or granted == "" or granted == "*":
        return True
    if requested is None:
        # Can't match a scoped grant when the check is unscoped — this
        # prevents "granted scope X but action asks about the whole type".
        return False
    return str(granted) == str(requested)


def is_effective_superadmin(user: User) -> bool:
    """Whether the user is a superadmin for gate-style checks.

    Two paths qualify:

    * **Legacy column** — ``User.is_superadmin=True`` set directly on the
      row (seeded ``admin`` account, anyone explicitly flagged in
      ``users/router.py``'s admin form).
    * **RBAC wildcard** — the user belongs to a group whose role carries
      a ``{action: "*", resource_type: "*"}`` permission (the built-in
      ``Superadmin`` role + any custom clone of it).

    Without the RBAC path, users provisioned via LDAP / OIDC / SAML and
    mapped into a Superadmin-role group pass every ``require_permission``
    gate but get 403 on the per-endpoint local ``_require_superadmin``
    helpers — closes that split-brain (issue #190).

    This is intentionally separate from :func:`user_has_permission`:
    inactive users are admitted here when the legacy flag is set so a
    disabled superadmin can still reach the diagnostic surfaces an
    operator might need during incident triage. Per-permission checks
    still gate on ``user.is_active``.
    """
    if getattr(user, "is_superadmin", False):
        return True
    return user_has_permission(user, "*", "*")


def user_has_permission(
    user: User,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None = None,
) -> bool:
    """Synchronous permission check.

    Call this from inside a handler once you've loaded the specific resource
    and have its UUID, to enforce per-row scoping. For coarse-grained gates
    use :func:`require_permission`.
    """
    if not user.is_active:
        return False
    if user.is_superadmin:
        return True

    req_rid = str(resource_id) if resource_id is not None else None

    for group in user.groups:
        for role in group.roles:
            for perm in role.permissions or []:
                if not isinstance(perm, dict):
                    continue
                if not _action_matches(perm.get("action", ""), action):
                    continue
                if not _resource_type_matches(perm.get("resource_type", ""), resource_type):
                    continue
                if not _resource_id_matches(perm.get("resource_id"), req_rid):
                    continue
                return True
    return False


async def _record_denial(
    db: AsyncSession,
    user: User,
    request: Request,
    action: str,
    resource_type: str,
    resource_id: str | None,
) -> None:
    """Write a `denied` audit row. Best-effort — never raises."""
    try:
        db.add(
            AuditLog(
                user_id=user.id,
                user_display_name=user.display_name,
                auth_source=getattr(user, "auth_source", "local") or "local",
                source_ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                action=action,
                resource_type=resource_type,
                resource_id=resource_id or "",
                resource_display=f"{resource_type}:{resource_id or '*'}",
                result="denied",
                error_detail=f"permission denied ({action} on {resource_type})",
            )
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — don't let audit failures block the 403
        logger.warning("permission_audit_write_failed", error=str(exc))


def require_permission(
    action: str,
    resource_type: str,
) -> Callable[..., Awaitable[User]]:
    """Return a FastAPI dependency that enforces a coarse-grained permission.

    Usage::

        @router.post("/spaces", dependencies=[Depends(require_permission("write", "ip_space"))])

    On denial it writes a `denied` audit row and raises 403. Superadmin always
    passes.
    """

    async def _dep(
        request: Request,
        current_user: CurrentUser,
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        if user_has_permission(current_user, action, resource_type):
            return current_user
        await _record_denial(db, current_user, request, action, resource_type, None)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: need '{action}' on '{resource_type}'",
        )

    return _dep


def require_any_permission(*pairs: tuple[str, str]) -> Callable[..., Awaitable[User]]:
    """Dependency that passes if the caller has ANY of (action, resource_type) pairs.

    Useful for endpoints that expose data drawn from multiple resource types
    (e.g. cross-module search).
    """

    async def _dep(
        request: Request,
        current_user: CurrentUser,
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        for action, resource_type in pairs:
            if user_has_permission(current_user, action, resource_type):
                return current_user
        # Denied — log against the first pair for traceability.
        action, resource_type = pairs[0] if pairs else ("read", "*")
        await _record_denial(db, current_user, request, action, resource_type, None)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied",
        )

    return _dep


# ── Router-level method → action mapper ────────────────────────────────────────
#
# Many routers have dozens of endpoints where the HTTP method already tells us
# the action (GET=read, POST/PUT/PATCH=write, DELETE=delete). Hand-annotating
# every endpoint with `dependencies=[Depends(require_permission(...))]` is
# noisy; instead we attach a single dependency at the router level that picks
# the action off the request method.


_METHOD_TO_ACTION = {
    "GET": "read",
    "HEAD": "read",
    "OPTIONS": "read",
    "POST": "write",
    "PUT": "write",
    "PATCH": "write",
    "DELETE": "delete",
}


def require_resource_permission(resource_type: str) -> Callable[..., Awaitable[User]]:
    """Router-level dependency that enforces permissions by HTTP method.

    The mapping is: GET/HEAD/OPTIONS → ``read``, POST/PUT/PATCH → ``write``,
    DELETE → ``delete``. Attach at the router scope::

        router = APIRouter(dependencies=[Depends(require_resource_permission("ip_space"))])

    Per-row checks should still be performed inside handlers that know the
    resource UUID, using :func:`user_has_permission`.
    """

    async def _dep(
        request: Request,
        current_user: CurrentUser,
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        action = _METHOD_TO_ACTION.get(request.method.upper(), "write")
        if user_has_permission(current_user, action, resource_type):
            return current_user
        await _record_denial(db, current_user, request, action, resource_type, None)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: need '{action}' on '{resource_type}'",
        )

    return _dep


def require_any_resource_permission(
    *resource_types: str,
) -> Callable[..., Awaitable[User]]:
    """Like :func:`require_resource_permission` but accepts multiple resource types.

    Passes if the user has the method-derived action on ANY of the listed
    resource types. Used by aggregate routers (e.g. IPAM has ip_space, ip_block,
    subnet, ip_address routes all under a single APIRouter, so we accept any of
    those four).
    """

    async def _dep(
        request: Request,
        current_user: CurrentUser,
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        action = _METHOD_TO_ACTION.get(request.method.upper(), "write")
        for rt in resource_types:
            if user_has_permission(current_user, action, rt):
                return current_user
        first = resource_types[0] if resource_types else "*"
        await _record_denial(db, current_user, request, action, first, None)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: need '{action}' on one of {list(resource_types)}",
        )

    return _dep


# ── Known synthetic permission resource_types ─────────────────────────────────
#
# Most resource_types map 1:1 to a real DB row (``ip_space``, ``dns_zone`` …),
# but a handful of admin-only surfaces use synthetic ``manage_*`` scopes that
# guard a feature rather than a row. Centralising the list here keeps the
# `_BUILTIN_ROLES` seed in `app.main` and the documentation in
# `docs/PERMISSIONS.md` honest as new ones land.
KNOWN_MANAGE_PERMISSIONS: frozenset[str] = frozenset(
    {
        "manage_dns_pools",
        "manage_domains",
        "manage_network_devices",
        "manage_nmap_scans",
    }
)


__all__ = [
    "KNOWN_MANAGE_PERMISSIONS",
    "require_any_permission",
    "require_any_resource_permission",
    "require_permission",
    "require_resource_permission",
    "user_has_permission",
]
