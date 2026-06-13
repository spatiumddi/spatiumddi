"""Regression tests for finding C4 (GHSA-mj4g-hw3m-62rm / issue #400).

Privilege ceiling on delegated role / group editing.

Before the fix, a non-superadmin holding a delegated ``admin:role``
(+``admin:group``) capability could mint a role carrying
``{action:"*", resource_type:"*"}`` (or any permission they don't themselves
hold) and self-assign it, escalating to effective superadmin. The fix adds
``app.core.permissions.caller_can_grant`` and enforces it in:

* roles router — ``create_role`` / ``update_role`` / ``clone_role``
* groups router — role attachment in ``create_group`` / ``update_group``
* time-bound grants — ``create_time_bound_grant``

These tests cover the helper directly plus the end-to-end HTTP enforcement,
and confirm legitimate flows (superadmin builds anything; a delegated admin
grants within their own ceiling) are NOT broken.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import caller_can_grant
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
    username: str,
    superadmin: bool = False,
) -> tuple[User, str]:
    role = Role(name=role_name, description="", is_builtin=False, permissions=permissions)
    group = Group(name=f"{role_name}-grp-{uuid.uuid4().hex[:6]}", description="")
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


# ── caller_can_grant unit tests ───────────────────────────────────────────────


def test_superadmin_can_grant_anything() -> None:
    u = _user(superadmin=True)
    assert caller_can_grant(u, [{"action": "*", "resource_type": "*"}]) is True
    assert caller_can_grant(u, [{"action": "admin", "resource_type": "anything"}]) is True


def test_effective_superadmin_via_wildcard_can_grant_anything() -> None:
    u = _user(superadmin=False)
    u.groups = [_group([_role([{"action": "*", "resource_type": "*"}])])]
    assert caller_can_grant(u, [{"action": "*", "resource_type": "*"}]) is True


def test_nonsuperadmin_cannot_grant_full_wildcard() -> None:
    """The core C4 escalation: a delegated admin minting {*, *}."""
    u = _user(superadmin=False)
    u.groups = [_group([_role([{"action": "admin", "resource_type": "role"}])])]
    assert caller_can_grant(u, [{"action": "*", "resource_type": "*"}]) is False


def test_nonsuperadmin_cannot_grant_action_wildcard() -> None:
    u = _user(superadmin=False)
    u.groups = [_group([_role([{"action": "admin", "resource_type": "subnet"}])])]
    # `{*, subnet}` would let the role do anything to subnets — wider than the
    # `admin` the caller holds in the action axis sense; still wildcard-minting.
    assert caller_can_grant(u, [{"action": "*", "resource_type": "subnet"}]) is False


def test_nonsuperadmin_cannot_grant_resource_wildcard() -> None:
    u = _user(superadmin=False)
    u.groups = [_group([_role([{"action": "read", "resource_type": "subnet"}])])]
    assert caller_can_grant(u, [{"action": "read", "resource_type": "*"}]) is False


def test_nonsuperadmin_cannot_grant_permission_they_lack() -> None:
    u = _user(superadmin=False)
    u.groups = [_group([_role([{"action": "admin", "resource_type": "subnet"}])])]
    # Caller has subnet admin but NOT dns_zone — cannot grant dns_zone.
    assert caller_can_grant(u, [{"action": "write", "resource_type": "dns_zone"}]) is False


def test_nonsuperadmin_can_grant_subset_of_own_perms() -> None:
    u = _user(superadmin=False)
    u.groups = [
        _group(
            [
                _role(
                    [
                        {"action": "admin", "resource_type": "subnet"},
                        {"action": "read", "resource_type": "dns_zone"},
                    ]
                )
            ]
        )
    ]
    # admin implies read/write/delete on subnet, so granting write:subnet is OK.
    assert caller_can_grant(u, [{"action": "write", "resource_type": "subnet"}]) is True
    assert caller_can_grant(u, [{"action": "read", "resource_type": "dns_zone"}]) is True
    # ...but anything outside the held set fails.
    assert (
        caller_can_grant(
            u,
            [
                {"action": "write", "resource_type": "subnet"},
                {"action": "write", "resource_type": "dns_zone"},  # not held
            ],
        )
        is False
    )


def test_malformed_entry_is_not_grantable() -> None:
    u = _user(superadmin=False)
    u.groups = [_group([_role([{"action": "*", "resource_type": "*"}])])]
    # Even an effective superadmin short-circuits to True, so use a plain admin.
    u2 = _user(superadmin=False)
    u2.groups = [_group([_role([{"action": "admin", "resource_type": "subnet"}])])]
    assert caller_can_grant(u2, ["not-a-dict"]) is False  # type: ignore[list-item]


def test_empty_perms_is_grantable() -> None:
    u = _user(superadmin=False)
    u.groups = [_group([_role([{"action": "admin", "resource_type": "subnet"}])])]
    assert caller_can_grant(u, []) is True


# ── HTTP enforcement — roles router ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delegated_admin_cannot_create_wildcard_role(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """C4 end-to-end: a non-superadmin with delegated admin:role is blocked
    from minting a {*, *} role."""
    _, token = await _persist_user_with_role(
        db_session,
        role_name="RoleAdmin-T",
        permissions=[{"action": "admin", "resource_type": "role"}],
        username="role-admin",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/roles",
        json={
            "name": "Sneaky-Superadmin",
            "permissions": [{"action": "*", "resource_type": "*"}],
        },
        headers=headers,
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_delegated_admin_cannot_create_role_with_unheld_perm(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _persist_user_with_role(
        db_session,
        role_name="RoleAdmin-Subnet-T",
        permissions=[
            {"action": "admin", "resource_type": "role"},
            {"action": "admin", "resource_type": "subnet"},
        ],
        username="role-admin-subnet",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    # Caller has no dns_zone permission → cannot author a role that grants it.
    r = await client.post(
        "/api/v1/roles",
        json={
            "name": "DNS-Grab",
            "permissions": [{"action": "admin", "resource_type": "dns_zone"}],
        },
        headers=headers,
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_delegated_admin_can_create_role_within_ceiling(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Legitimate delegation still works: a delegated admin authors a role
    carrying only perms they themselves hold."""
    _, token = await _persist_user_with_role(
        db_session,
        role_name="RoleAdmin-WithSubnet-T",
        permissions=[
            {"action": "admin", "resource_type": "role"},
            {"action": "admin", "resource_type": "subnet"},
        ],
        username="role-admin-ok",
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/roles",
        json={
            "name": "Subnet-Reader",
            "permissions": [{"action": "read", "resource_type": "subnet"}],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_superadmin_can_create_wildcard_role(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The platform owner is unbounded — the ceiling must not break this."""
    _, token = await _persist_user_with_role(
        db_session,
        role_name="Real-Superadmin-T",
        permissions=[{"action": "*", "resource_type": "*"}],
        username="real-superadmin",
        superadmin=True,
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/roles",
        json={
            "name": "Custom-Superadmin",
            "permissions": [{"action": "*", "resource_type": "*"}],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text


# ── HTTP enforcement — groups router (role attachment) ─────────────────────────


@pytest.mark.asyncio
async def test_delegated_group_admin_cannot_attach_superadmin_role(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """C4 via the groups path: a non-superadmin with admin:group cannot attach
    a pre-existing {*, *} (Superadmin-equivalent) role to a group."""
    _, token = await _persist_user_with_role(
        db_session,
        role_name="GroupAdmin-T",
        permissions=[{"action": "admin", "resource_type": "group"}],
        username="group-admin",
    )
    # A wildcard role already exists in the system (e.g. built-in Superadmin).
    wildcard_role = Role(
        name=f"Wildcard-{uuid.uuid4().hex[:6]}",
        description="",
        is_builtin=False,
        permissions=[{"action": "*", "resource_type": "*"}],
    )
    db_session.add(wildcard_role)
    await db_session.flush()
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/groups",
        json={
            "name": "Escalation-Group",
            "role_ids": [str(wildcard_role.id)],
        },
        headers=headers,
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_superadmin_can_attach_wildcard_role_to_group(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _persist_user_with_role(
        db_session,
        role_name="GroupSuperadmin-T",
        permissions=[{"action": "*", "resource_type": "*"}],
        username="group-superadmin",
        superadmin=True,
    )
    wildcard_role = Role(
        name=f"Wildcard2-{uuid.uuid4().hex[:6]}",
        description="",
        is_builtin=False,
        permissions=[{"action": "*", "resource_type": "*"}],
    )
    db_session.add(wildcard_role)
    await db_session.flush()
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/groups",
        json={
            "name": "Legit-Admin-Group",
            "role_ids": [str(wildcard_role.id)],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text


# ── HTTP enforcement — time-bound grants ───────────────────────────────────────


@pytest.mark.asyncio
async def test_delegated_admin_cannot_mint_wildcard_time_bound_grant(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """C4 via the time-bound-grant path (docstring previously noted no
    escalation guard)."""
    _, token = await _persist_user_with_role(
        db_session,
        role_name="GrantAdmin-T",
        permissions=[{"action": "admin", "resource_type": "group"}],
        username="grant-admin",
    )
    target_group = Group(name=f"Target-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(target_group)
    await db_session.flush()
    gid = str(target_group.id)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/groups/time-bound-grants",
        json={
            "group_id": gid,
            "action": "*",
            "resource_type": "*",
            "expires_at": "2099-01-01T00:00:00Z",
            "reason": "escalation attempt",
        },
        headers=headers,
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_superadmin_can_mint_time_bound_grant(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _persist_user_with_role(
        db_session,
        role_name="GrantSuperadmin-T",
        permissions=[{"action": "*", "resource_type": "*"}],
        username="grant-superadmin",
        superadmin=True,
    )
    target_group = Group(name=f"Target2-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(target_group)
    await db_session.flush()
    gid = str(target_group.id)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/groups/time-bound-grants",
        json={
            "group_id": gid,
            "action": "admin",
            "resource_type": "subnet",
            "expires_at": "2099-01-01T00:00:00Z",
            "reason": "legit temporary access",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
