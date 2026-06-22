"""Self-governance lock tests (#62).

The lock (``PlatformSettings.approvals_protect_controls``, opt-in at
enable time) makes WEAKENING the approval control plane require a SECOND
superadmin's approval, with a superadmin break-glass escape hatch so the
platform can never be permanently locked out.

Covered:

* flag-on → PATCH disable ``governance.approvals`` returns 202 + a pending
  change_request; the module stays enabled.
* a DIFFERENT superadmin approves → module disabled.
* a non-superadmin holding ``{approve, change_request}`` CANNOT approve a
  control op (403).
* flag-on → disabling a policy / deleting a policy / lowering
  applies_to_superadmin / unlocking each returns 202 (gated).
* break-glass (superadmin + correct password + correct phrase) force-disables
  immediately + writes the high-severity ``approvals.break_glass`` audit row;
  wrong phrase → 422; wrong password → 403.
* flag-off → every control op executes inline single-person, byte-identical
  to today (no change_request row created).
* enabling protection at enable time (``protect_controls=true`` with
  ``enabled=true``) is single-person/inline + audited.

Mirrors ``test_approval_workflows.py``'s shared-session fixture model — the
``client`` + ``db_session`` fixtures share one session, so a fixture write
is visible to the handler and a post-call read sees the handler's commit.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import Group, Role, User
from app.models.change_request import ApprovalPolicy, ChangeRequest
from app.models.feature_module import FeatureModule
from app.models.settings import PlatformSettings
from app.services import feature_modules

# ── Helpers ────────────────────────────────────────────────────────────────


async def _user(
    db: AsyncSession,
    *,
    name: str,
    permissions: list[dict] | None = None,
    superadmin: bool = False,
    password: str = "password123",
) -> tuple[User, str]:
    user = User(
        username=f"{name}-{uuid.uuid4().hex[:8]}",
        email=f"{name}-{uuid.uuid4().hex[:8]}@t.io",
        display_name=name,
        hashed_password=hash_password(password),
        is_superadmin=superadmin,
    )
    user.groups = []
    if permissions:
        role = Role(name=f"role-{uuid.uuid4().hex[:8]}", permissions=permissions)
        group = Group(name=f"grp-{uuid.uuid4().hex[:8]}")
        group.roles = [role]
        user.groups = [group]
        db.add_all([role, group])
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _enable_module(db: AsyncSession, *, enabled: bool = True) -> None:
    existing = await db.get(FeatureModule, "governance.approvals")
    if existing is None:
        db.add(FeatureModule(id="governance.approvals", enabled=enabled))
    else:
        existing.enabled = enabled
    await db.flush()
    feature_modules.invalidate_cache()


async def _settings(db: AsyncSession) -> PlatformSettings:
    ps = await db.get(PlatformSettings, 1)
    if ps is None:
        ps = PlatformSettings(id=1)
        db.add(ps)
        await db.flush()
    return ps


async def _set_lock(db: AsyncSession, *, on: bool) -> None:
    ps = await _settings(db)
    ps.approvals_protect_controls = on
    await db.flush()


async def _policy(
    db: AsyncSession,
    *,
    enabled: bool = True,
    applies_to_superadmin: bool = True,
    is_builtin: bool = False,
    name: str | None = None,
) -> ApprovalPolicy:
    p = ApprovalPolicy(
        name=name or f"pol-{uuid.uuid4().hex[:6]}",
        resource_type="subnet",
        action="delete",
        min_count=None,
        enabled=enabled,
        applies_to_superadmin=applies_to_superadmin,
        ttl_hours=168,
        is_builtin=is_builtin,
    )
    db.add(p)
    await db.flush()
    return p


# ── flag-on: disable module → 202, second superadmin approves → disabled ─────


@pytest.mark.asyncio
async def test_locked_disable_module_queues_then_approved(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    _, tb = await _user(db_session, name="super-b", superadmin=True)
    await db_session.commit()

    # Disable attempt is gated → 202 + pending CR; module still enabled.
    r = await client.patch(
        "/api/v1/admin/feature-modules/governance.approvals",
        headers=_auth(ta),
        json={"enabled": False},
    )
    assert r.status_code == 202, r.text
    cr_id = r.json()["change_request_id"]
    assert r.json()["state"] == "pending"
    assert await feature_modules.is_module_enabled(db_session, "governance.approvals")

    cr = await db_session.get(ChangeRequest, uuid.UUID(cr_id))
    assert cr is not None and cr.operation == "modify_approval_control"

    # A DIFFERENT superadmin approves → module disabled.
    r2 = await client.post(f"/api/v1/change-requests/{cr_id}/approve", headers=_auth(tb), json={})
    assert r2.status_code == 200, r2.text
    assert r2.json()["state"] == "executed"
    feature_modules.invalidate_cache()
    assert not await feature_modules.is_module_enabled(db_session, "governance.approvals")


@pytest.mark.asyncio
async def test_non_superadmin_cannot_approve_control_op(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A non-superadmin holding {approve, change_request} + {admin,
    approval_control} still cannot approve a control op (403)."""
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    _, approver_t = await _user(
        db_session,
        name="approver",
        permissions=[
            {"action": "approve", "resource_type": "change_request"},
            {"action": "read", "resource_type": "change_request"},
            {"action": "admin", "resource_type": "approval_control"},
        ],
    )
    await db_session.commit()

    cr_id = (
        await client.patch(
            "/api/v1/admin/feature-modules/governance.approvals",
            headers=_auth(ta),
            json={"enabled": False},
        )
    ).json()["change_request_id"]

    r = await client.post(
        f"/api/v1/change-requests/{cr_id}/approve", headers=_auth(approver_t), json={}
    )
    assert r.status_code == 403, r.text
    assert "superadmin" in r.json()["detail"].lower()
    cr = await db_session.get(ChangeRequest, uuid.UUID(cr_id))
    assert cr is not None and cr.state == "pending"


# ── flag-on: weaken-policy + unlock all gated (202) ──────────────────────────


@pytest.mark.asyncio
async def test_locked_disable_policy_gated(client: AsyncClient, db_session: AsyncSession) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    p = await _policy(db_session, enabled=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.put(
        f"/api/v1/change-requests/policies/{p.id}",
        headers=_auth(ta),
        json={"enabled": False},
    )
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "pending"
    await db_session.refresh(p)
    assert p.enabled is True  # not weakened inline


@pytest.mark.asyncio
async def test_locked_delete_policy_gated(client: AsyncClient, db_session: AsyncSession) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    p = await _policy(db_session, is_builtin=False)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.delete(f"/api/v1/change-requests/policies/{p.id}", headers=_auth(ta))
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "pending"
    assert await db_session.get(ApprovalPolicy, p.id) is not None  # not deleted inline


@pytest.mark.asyncio
async def test_locked_lower_superadmin_gate_gated(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    p = await _policy(db_session, applies_to_superadmin=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.put(
        f"/api/v1/change-requests/policies/{p.id}",
        headers=_auth(ta),
        json={"applies_to_superadmin": False},
    )
    assert r.status_code == 202, r.text
    await db_session.refresh(p)
    assert p.applies_to_superadmin is True


@pytest.mark.asyncio
async def test_locked_unlock_gated(client: AsyncClient, db_session: AsyncSession) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.post(
        "/api/v1/admin/feature-modules/approvals-lock",
        headers=_auth(ta),
        json={"enabled": False},
    )
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "pending"
    ps = await db_session.get(PlatformSettings, 1)
    assert ps is not None and ps.approvals_protect_controls is True  # still locked


# ── break-glass ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_break_glass_force_disables(client: AsyncClient, db_session: AsyncSession) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True, password="hunter2!")
    await db_session.commit()

    r = await client.post(
        "/api/v1/admin/feature-modules/break-glass",
        headers=_auth(ta),
        json={"kind": "disable_module", "password": "hunter2!", "confirm_phrase": "BREAK GLASS"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["forced"] is True
    feature_modules.invalidate_cache()
    assert not await feature_modules.is_module_enabled(db_session, "governance.approvals")

    # High-severity break-glass audit row landed.
    n = (
        await db_session.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "approvals.break_glass",
                AuditLog.resource_type == "approval_control",
                AuditLog.result == "success",
            )
        )
    ).scalar_one()
    assert n == 1


@pytest.mark.asyncio
async def test_break_glass_wrong_phrase_422(client: AsyncClient, db_session: AsyncSession) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True, password="hunter2!")
    await db_session.commit()

    r = await client.post(
        "/api/v1/admin/feature-modules/break-glass",
        headers=_auth(ta),
        json={"kind": "disable_module", "password": "hunter2!", "confirm_phrase": "nope"},
    )
    assert r.status_code == 422, r.text
    assert await feature_modules.is_module_enabled(db_session, "governance.approvals")


@pytest.mark.asyncio
async def test_break_glass_wrong_password_403(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True, password="hunter2!")
    await db_session.commit()

    r = await client.post(
        "/api/v1/admin/feature-modules/break-glass",
        headers=_auth(ta),
        json={"kind": "disable_module", "password": "WRONG", "confirm_phrase": "BREAK GLASS"},
    )
    assert r.status_code == 403, r.text
    assert await feature_modules.is_module_enabled(db_session, "governance.approvals")


# ── flag-off: every control op inline single-person, no CR row ───────────────


@pytest.mark.asyncio
async def test_flag_off_disable_module_inline(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=False)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.patch(
        "/api/v1/admin/feature-modules/governance.approvals",
        headers=_auth(ta),
        json={"enabled": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    n_cr = (await db_session.execute(select(func.count(ChangeRequest.id)))).scalar_one()
    assert n_cr == 0


@pytest.mark.asyncio
async def test_flag_off_delete_policy_inline(client: AsyncClient, db_session: AsyncSession) -> None:
    await _enable_module(db_session)
    await _set_lock(db_session, on=False)
    p = await _policy(db_session, is_builtin=False)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.delete(f"/api/v1/change-requests/policies/{p.id}", headers=_auth(ta))
    assert r.status_code == 204, r.text
    assert await db_session.get(ApprovalPolicy, p.id) is None
    n_cr = (await db_session.execute(select(func.count(ChangeRequest.id)))).scalar_one()
    assert n_cr == 0


# ── set-at-enable-time is single-person inline + audited ─────────────────────


@pytest.mark.asyncio
async def test_enable_with_protect_controls_inline(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Start disabled + unlocked.
    await _enable_module(db_session, enabled=False)
    await _set_lock(db_session, on=False)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.patch(
        "/api/v1/admin/feature-modules/governance.approvals",
        headers=_auth(ta),
        json={"enabled": True, "protect_controls": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True

    ps = await db_session.get(PlatformSettings, 1)
    assert ps is not None and ps.approvals_protect_controls is True
    # No change_request — strengthening stays single-person.
    n_cr = (await db_session.execute(select(func.count(ChangeRequest.id)))).scalar_one()
    assert n_cr == 0
    # Lock-set audit row written.
    n = (
        await db_session.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.resource_type == "platform_settings",
                AuditLog.resource_id == "approvals_protect_controls",
            )
        )
    ).scalar_one()
    assert n == 1
