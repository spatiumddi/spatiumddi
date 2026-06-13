"""API tests for time-bound-grant CRUD (#65).

* POST requires ``admin`` on ``group`` (403 for a read-only user) and audits.
* GET requires ``read`` on ``group``.
* DELETE soft-revokes (sets ``revoked_at``) + audits.
* Create rejects an invalid action / empty resource_type.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import Group, Role, User
from app.models.time_bound_grant import TimeBoundGrant


async def _user_with_perms(
    db: AsyncSession, *, permissions: list[dict[str, Any]], username: str
) -> tuple[User, str]:
    role = Role(name=f"r-{uuid.uuid4().hex[:6]}", description="", permissions=permissions)
    group = Group(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    user = User(
        username=username,
        email=f"{username}-{uuid.uuid4().hex[:4]}@t.io",
        display_name=username,
        hashed_password=hash_password("password123"),
        is_superadmin=False,
    )
    group.roles = [role]
    group.users = [user]
    db.add_all([role, group, user])
    await db.flush()
    await db.commit()
    return user, create_access_token(str(user.id))


async def _target_group(db: AsyncSession) -> Group:
    g = Group(name=f"target-{uuid.uuid4().hex[:6]}", description="")
    db.add(g)
    await db.flush()
    await db.commit()
    return g


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=2)).isoformat()


async def test_create_requires_admin_on_group(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Read-only-on-group user → 403 on POST.
    _, token = await _user_with_perms(
        db_session, permissions=[{"action": "read", "resource_type": "group"}], username="reader"
    )
    target = await _target_group(db_session)
    r = await client.post(
        "/api/v1/groups/time-bound-grants",
        json={
            "group_id": str(target.id),
            "action": "write",
            "resource_type": "subnet",
            "expires_at": _future(),
            "reason": "ticket-123",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403, r.text


async def test_create_and_audit(client: AsyncClient, db_session: AsyncSession) -> None:
    # The caller needs ``admin`` on ``group`` to reach the endpoint AND must
    # itself hold the permission it grants — the #400/C4 privilege ceiling
    # (caller_can_grant) forbids a delegated group-admin from minting a
    # time-bound grant for a permission they don't possess (an escalation
    # path). So admin1 holds ``write:subnet`` too.
    user, token = await _user_with_perms(
        db_session,
        permissions=[
            {"action": "admin", "resource_type": "group"},
            {"action": "write", "resource_type": "subnet"},
        ],
        username="admin1",
    )
    target = await _target_group(db_session)
    r = await client.post(
        "/api/v1/groups/time-bound-grants",
        json={
            "group_id": str(target.id),
            "action": "write",
            "resource_type": "subnet",
            "resource_id": None,
            "expires_at": _future(),
            "reason": "ticket-123",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["action"] == "write"
    assert body["resource_type"] == "subnet"
    assert body["is_active"] is True
    grant_id = body["id"]

    # Audit row written with action=permission_change.
    audit = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.resource_type == "time_bound_grant")
            .where(AuditLog.resource_id == grant_id)
            .where(AuditLog.action == "permission_change")
        )
    ).scalar_one_or_none()
    assert audit is not None
    assert audit.user_id == user.id


async def test_get_requires_read_on_group(client: AsyncClient, db_session: AsyncSession) -> None:
    # A user with no group perms at all → 403 on GET.
    _, token = await _user_with_perms(
        db_session,
        permissions=[{"action": "read", "resource_type": "subnet"}],
        username="nogroup",
    )
    r = await client.get(
        "/api/v1/groups/time-bound-grants",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403, r.text

    # A read-on-group user → 200.
    _, token2 = await _user_with_perms(
        db_session,
        permissions=[{"action": "read", "resource_type": "group"}],
        username="grpreader",
    )
    r2 = await client.get(
        "/api/v1/groups/time-bound-grants",
        headers={"Authorization": f"Bearer {token2}"},
    )
    assert r2.status_code == 200, r2.text


async def test_revoke_now_sets_revoked_at_and_audits(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, token = await _user_with_perms(
        db_session, permissions=[{"action": "admin", "resource_type": "group"}], username="admin2"
    )
    target = await _target_group(db_session)
    grant = TimeBoundGrant(
        group_id=target.id,
        action="write",
        resource_type="subnet",
        resource_id=None,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        reason="to-be-revoked",
        granted_by_user_id=user.id,
    )
    db_session.add(grant)
    await db_session.flush()
    await db_session.commit()

    r = await client.delete(
        f"/api/v1/groups/time-bound-grants/{grant.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revoked_at"] is not None
    assert body["is_active"] is False

    await db_session.refresh(grant)
    assert grant.revoked_at is not None

    audit = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.resource_id == str(grant.id))
            .where(AuditLog.action == "permission_change")
            .where(AuditLog.new_value["reason"].astext == "manual_revoke")
        )
    ).scalar_one_or_none()
    assert audit is not None


async def test_revoke_requires_admin(client: AsyncClient, db_session: AsyncSession) -> None:
    user, _ = await _user_with_perms(
        db_session, permissions=[{"action": "admin", "resource_type": "group"}], username="admin3"
    )
    target = await _target_group(db_session)
    grant = TimeBoundGrant(
        group_id=target.id,
        action="write",
        resource_type="subnet",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        reason="x",
        granted_by_user_id=user.id,
    )
    db_session.add(grant)
    await db_session.flush()
    await db_session.commit()

    _, reader_token = await _user_with_perms(
        db_session, permissions=[{"action": "read", "resource_type": "group"}], username="reader2"
    )
    r = await client.delete(
        f"/api/v1/groups/time-bound-grants/{grant.id}",
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert r.status_code == 403, r.text


async def test_create_rejects_invalid_action(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _user_with_perms(
        db_session, permissions=[{"action": "admin", "resource_type": "group"}], username="admin4"
    )
    target = await _target_group(db_session)
    r = await client.post(
        "/api/v1/groups/time-bound-grants",
        json={
            "group_id": str(target.id),
            "action": "frobnicate",  # invalid
            "resource_type": "subnet",
            "expires_at": _future(),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422, r.text


async def test_create_rejects_empty_resource_type(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _user_with_perms(
        db_session, permissions=[{"action": "admin", "resource_type": "group"}], username="admin5"
    )
    target = await _target_group(db_session)
    r = await client.post(
        "/api/v1/groups/time-bound-grants",
        json={
            "group_id": str(target.id),
            "action": "write",
            "resource_type": "   ",  # whitespace → empty after strip
            "expires_at": _future(),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422, r.text
