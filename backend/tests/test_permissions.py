"""Unit tests for the permission helper + route-level RBAC enforcement."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import is_effective_superadmin, user_has_permission
from app.core.security import create_access_token, hash_password
from app.models.auth import Group, Role, User

# ── Helpers ───────────────────────────────────────────────────────────────────


def _user(superadmin: bool = False, is_active: bool = True) -> User:
    u = User(
        username=f"u{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@t.io",
        display_name="Test",
        hashed_password="x",
        is_superadmin=superadmin,
        is_active=is_active,
    )
    # Collections on a detached User default to None; the helper iterates them.
    u.groups = []
    return u


def _role(perms: list[dict[str, Any]]) -> Role:
    return Role(name=f"r{uuid.uuid4().hex[:6]}", description="", permissions=perms)


def _group(roles: list[Role]) -> Group:
    g = Group(name=f"g{uuid.uuid4().hex[:6]}", description="")
    g.roles = roles
    g.users = []
    return g


async def _persist_user_with_role(
    db: AsyncSession,
    *,
    role_name: str,
    permissions: list[dict[str, Any]],
    username: str = "rbac-user",
    superadmin: bool = False,
) -> tuple[User, str]:
    role = Role(name=role_name, description="", is_builtin=False, permissions=permissions)
    group = Group(name=f"{role_name}-grp", description="")
    user = User(
        username=username,
        email=f"{username}@t.io",
        display_name=username,
        hashed_password=hash_password("password123"),
        is_superadmin=superadmin,
    )
    group.roles = [role]
    group.users = [user]
    db.add_all([role, group, user])
    await db.flush()
    token = create_access_token(str(user.id))
    return user, token


# ── Unit tests ────────────────────────────────────────────────────────────────


def test_superadmin_short_circuits() -> None:
    u = _user(superadmin=True)
    # No roles at all
    assert user_has_permission(u, "write", "subnet") is True
    assert user_has_permission(u, "delete", "any_made_up_type") is True


# ── is_effective_superadmin (issue #190) ──────────────────────────────────────


def test_effective_superadmin_legacy_flag() -> None:
    """User with the legacy ``is_superadmin`` column set is admitted."""
    u = _user(superadmin=True)
    assert is_effective_superadmin(u) is True


def test_effective_superadmin_via_wildcard_permission() -> None:
    """OIDC / LDAP user mapped into a group with the built-in Superadmin
    role gets the same admission as a legacy ``is_superadmin=True``.

    This is the bug #190 closes: pre-fix, per-endpoint local
    ``_require_superadmin`` helpers ignored this path entirely.
    """
    u = _user(superadmin=False)
    u.groups = [_group([_role([{"action": "*", "resource_type": "*"}])])]
    assert is_effective_superadmin(u) is True


def test_effective_superadmin_denied_without_either_path() -> None:
    """No legacy flag, no wildcard permission → denied."""
    u = _user(superadmin=False)
    u.groups = [_group([_role([{"action": "read", "resource_type": "*"}])])]
    assert is_effective_superadmin(u) is False


def test_effective_superadmin_denied_with_no_groups() -> None:
    u = _user(superadmin=False)
    assert is_effective_superadmin(u) is False


def test_effective_superadmin_legacy_flag_overrides_inactive() -> None:
    """A disabled legacy superadmin can still pass the gate — matches the
    docstring on :func:`is_effective_superadmin` ("disabled superadmin can
    still reach diagnostic surfaces during incident triage"). The
    wildcard-permission path still gates on ``is_active``.
    """
    u = _user(superadmin=True, is_active=False)
    assert is_effective_superadmin(u) is True


def test_effective_superadmin_wildcard_path_respects_inactive() -> None:
    u = _user(superadmin=False, is_active=False)
    u.groups = [_group([_role([{"action": "*", "resource_type": "*"}])])]
    assert is_effective_superadmin(u) is False


def test_inactive_user_denied_even_with_wildcard() -> None:
    u = _user(is_active=False)
    u.groups = [_group([_role([{"action": "*", "resource_type": "*"}])])]
    assert user_has_permission(u, "read", "subnet") is False


def test_empty_groups_denied() -> None:
    u = _user()
    assert user_has_permission(u, "read", "subnet") is False


def test_wildcard_action_and_type() -> None:
    u = _user()
    u.groups = [_group([_role([{"action": "*", "resource_type": "*"}])])]
    assert user_has_permission(u, "write", "subnet") is True
    assert user_has_permission(u, "delete", "dns_zone") is True


def test_viewer_matches_read_but_not_write() -> None:
    u = _user()
    u.groups = [_group([_role([{"action": "read", "resource_type": "*"}])])]
    assert user_has_permission(u, "read", "subnet") is True
    assert user_has_permission(u, "write", "subnet") is False
    assert user_has_permission(u, "delete", "ip_space") is False


def test_admin_action_implies_read_write_delete() -> None:
    u = _user()
    u.groups = [_group([_role([{"action": "admin", "resource_type": "subnet"}])])]
    assert user_has_permission(u, "read", "subnet") is True
    assert user_has_permission(u, "write", "subnet") is True
    assert user_has_permission(u, "delete", "subnet") is True
    # Does NOT cross resource types.
    assert user_has_permission(u, "read", "dns_zone") is False


def test_resource_id_scope_matches_only_the_specific_uuid() -> None:
    target = str(uuid.uuid4())
    other = str(uuid.uuid4())
    u = _user()
    u.groups = [
        _group(
            [
                _role(
                    [
                        {
                            "action": "write",
                            "resource_type": "subnet",
                            "resource_id": target,
                        }
                    ]
                )
            ]
        )
    ]
    assert user_has_permission(u, "write", "subnet", target) is True
    # UUID object should also work (common case in handlers).
    assert user_has_permission(u, "write", "subnet", uuid.UUID(target)) is True
    # Different UUID does not match.
    assert user_has_permission(u, "write", "subnet", other) is False
    # Unscoped check can't be satisfied by a scoped grant.
    assert user_has_permission(u, "write", "subnet") is False


def test_unscoped_grant_covers_scoped_check() -> None:
    u = _user()
    u.groups = [_group([_role([{"action": "admin", "resource_type": "subnet"}])])]
    assert user_has_permission(u, "write", "subnet", uuid.uuid4()) is True


def test_malformed_permission_entry_is_ignored() -> None:
    u = _user()
    # Rows that aren't dicts, or lack keys, should not crash and should not grant.
    u.groups = [
        _group(
            [
                _role(
                    [
                        "not-a-dict",  # type: ignore[list-item]
                        {"action": "write"},  # missing resource_type
                    ]
                )
            ]
        )
    ]
    assert user_has_permission(u, "write", "subnet") is False


# ── HTTP route enforcement tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_viewer_can_get_but_not_post_subnets(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _persist_user_with_role(
        db_session,
        role_name="Viewer",
        permissions=[{"action": "read", "resource_type": "*"}],
        username="viewer-user",
    )
    headers = {"Authorization": f"Bearer {token}"}

    # GET /spaces: should succeed (read on ip_space matches via wildcard).
    r = await client.get("/api/v1/ipam/spaces", headers=headers)
    assert r.status_code == 200

    # POST /spaces: viewer has no write grant → 403.
    r = await client.post(
        "/api/v1/ipam/spaces",
        json={"name": "Nope", "description": "should fail"},
        headers=headers,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_ipam_editor_can_post_subnets(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _persist_user_with_role(
        db_session,
        role_name="IPAM-Editor-T",
        permissions=[
            {"action": "admin", "resource_type": "ip_space"},
            {"action": "admin", "resource_type": "ip_block"},
            {"action": "admin", "resource_type": "subnet"},
        ],
        username="ipam-editor",
    )
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/api/v1/ipam/spaces",
        json={"name": "Editable", "description": "ok"},
        headers=headers,
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_no_permissions_user_is_denied(client: AsyncClient, db_session: AsyncSession) -> None:
    user = User(
        username="no-perms",
        email="np@t.io",
        display_name="No Perms",
        hashed_password=hash_password("password123"),
        is_superadmin=False,
    )
    db_session.add(user)
    await db_session.flush()
    token = create_access_token(str(user.id))
    headers = {"Authorization": f"Bearer {token}"}

    # GET is read → denied.
    r = await client.get("/api/v1/ipam/spaces", headers=headers)
    assert r.status_code == 403
    # POST is write → denied.
    r = await client.post("/api/v1/ipam/spaces", json={"name": "X"}, headers=headers)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_dns_editor_cannot_touch_ipam(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _persist_user_with_role(
        db_session,
        role_name="DNS-Editor-T",
        permissions=[
            {"action": "admin", "resource_type": "dns_zone"},
            {"action": "admin", "resource_type": "dns_record"},
            {"action": "admin", "resource_type": "dns_group"},
        ],
        username="dns-editor",
    )
    headers = {"Authorization": f"Bearer {token}"}

    # DNS groups: OK
    r = await client.get("/api/v1/dns/groups", headers=headers)
    assert r.status_code == 200
    # IPAM: denied
    r = await client.get("/api/v1/ipam/spaces", headers=headers)
    assert r.status_code == 403
