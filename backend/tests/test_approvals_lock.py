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


# ── #1: enabling the lock when NO PlatformSettings row exists persists it ─────


@pytest.mark.asyncio
async def test_enable_lock_with_no_settings_row_persists_then_gates(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#1 CRITICAL regression: turning the lock ON via the dedicated endpoint
    on a DB that has NO ``PlatformSettings(1)`` row must CREATE the row and
    persist ``approvals_protect_controls=True`` (previously the write was
    silently skipped + the API reported the lock ON while it read OFF, so every
    weakening op ran ungated). Then prove a weakening op is actually gated."""
    await _enable_module(db_session)
    # Ensure there is genuinely no settings row.
    existing = await db_session.get(PlatformSettings, 1)
    if existing is not None:
        await db_session.delete(existing)
        await db_session.flush()
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()
    assert await db_session.get(PlatformSettings, 1) is None

    r = await client.post(
        "/api/v1/admin/feature-modules/approvals-lock",
        headers=_auth(ta),
        json={"enabled": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["approvals_protect_controls"] is True

    # The row now exists AND the flag actually persisted.
    ps = await db_session.get(PlatformSettings, 1)
    assert ps is not None and ps.approvals_protect_controls is True

    # And a weakening op is now genuinely gated (proves the lock READS on).
    r2 = await client.patch(
        "/api/v1/admin/feature-modules/governance.approvals",
        headers=_auth(ta),
        json={"enabled": False},
    )
    assert r2.status_code == 202, r2.text
    assert await feature_modules.is_module_enabled(db_session, "governance.approvals")


@pytest.mark.asyncio
async def test_enable_at_enable_time_with_no_settings_row_persists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#1 companion: the set-at-enable-time path (PATCH enabled=true +
    protect_controls=true) must also get-or-create the settings row."""
    await _enable_module(db_session, enabled=False)
    existing = await db_session.get(PlatformSettings, 1)
    if existing is not None:
        await db_session.delete(existing)
        await db_session.flush()
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()
    assert await db_session.get(PlatformSettings, 1) is None

    r = await client.patch(
        "/api/v1/admin/feature-modules/governance.approvals",
        headers=_auth(ta),
        json={"enabled": True, "protect_controls": True},
    )
    assert r.status_code == 200, r.text
    ps = await db_session.get(PlatformSettings, 1)
    assert ps is not None and ps.approvals_protect_controls is True


# ── #2: coverage-reducing policy edits are gated (side-door closed) ───────────


@pytest.mark.asyncio
async def test_locked_repoint_resource_type_gated(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#2 CRITICAL regression: while locked, a PUT that repoints
    ``resource_type`` to a different (non-matching) pair while keeping
    enabled=true is a COVERAGE-REDUCING edit and must be gated (202), not run
    inline. Previously this was the side-door to neuter a live-looking policy."""
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    # Start on a gateable pair; repoint to another gateable pair so
    # _validate_gateable passes and only the coverage-reduction gating fires.
    p = await _policy(db_session, enabled=True)  # subnet/delete
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.put(
        f"/api/v1/change-requests/policies/{p.id}",
        headers=_auth(ta),
        json={"resource_type": "dns_zone"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "pending"
    await db_session.refresh(p)
    assert p.resource_type == "subnet"  # not repointed inline


@pytest.mark.asyncio
async def test_locked_inflate_min_count_gated(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#2: while locked, raising ``min_count`` (gates fewer ops) is gated."""
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    p = await _policy(db_session, enabled=True)
    p.min_count = 5
    await db_session.flush()
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.put(
        f"/api/v1/change-requests/policies/{p.id}",
        headers=_auth(ta),
        json={"min_count": 500},
    )
    assert r.status_code == 202, r.text
    await db_session.refresh(p)
    assert p.min_count == 5  # not inflated inline


@pytest.mark.asyncio
async def test_locked_min_count_null_to_value_gated(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#2: while locked, setting ``min_count`` NULL→value (NULL = always-gate =
    strongest) is coverage-reducing and gated."""
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    p = await _policy(db_session, enabled=True)  # min_count starts None
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.put(
        f"/api/v1/change-requests/policies/{p.id}",
        headers=_auth(ta),
        json={"min_count": 10},
    )
    assert r.status_code == 202, r.text
    await db_session.refresh(p)
    assert p.min_count is None  # not weakened inline


@pytest.mark.asyncio
async def test_locked_strengthening_edit_stays_inline(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#2 negative: a STRENGTHENING / neutral edit (lower min_count, rename)
    while locked stays inline (200) — only coverage-reducing edits gate."""
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    p = await _policy(db_session, enabled=True)
    p.min_count = 100
    await db_session.flush()
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.put(
        f"/api/v1/change-requests/policies/{p.id}",
        headers=_auth(ta),
        json={"min_count": 5, "name": "tighter"},
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(p)
    assert p.min_count == 5 and p.name == "tighter"
    n_cr = (await db_session.execute(select(func.count(ChangeRequest.id)))).scalar_one()
    assert n_cr == 0


@pytest.mark.asyncio
async def test_repoint_resource_type_replays_on_approve(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#2: the gated repoint edit, once approved by a SECOND superadmin, REPLAYS
    the exact edit under the approver."""
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    p = await _policy(db_session, enabled=True)  # subnet/delete
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    _, tb = await _user(db_session, name="super-b", superadmin=True)
    await db_session.commit()

    cr_id = (
        await client.put(
            f"/api/v1/change-requests/policies/{p.id}",
            headers=_auth(ta),
            json={"resource_type": "dns_zone"},
        )
    ).json()["change_request_id"]

    r = await client.post(f"/api/v1/change-requests/{cr_id}/approve", headers=_auth(tb), json={})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "executed"
    await db_session.refresh(p)
    assert p.resource_type == "dns_zone"  # edit replayed


@pytest.mark.asyncio
async def test_min_count_over_bound_422(client: AsyncClient, db_session: AsyncSession) -> None:
    """#2: ``min_count`` above the new upper bound is rejected at the schema
    layer (422) — closes the 'inflate to astronomical so it gates nothing'
    side-door before it can even reach the gating logic."""
    await _enable_module(db_session)
    await _set_lock(db_session, on=False)
    p = await _policy(db_session, enabled=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    await db_session.commit()

    r = await client.put(
        f"/api/v1/change-requests/policies/{p.id}",
        headers=_auth(ta),
        json={"min_count": 2_000_000},
    )
    assert r.status_code == 422, r.text
    # Create path bounded too.
    r2 = await client.post(
        "/api/v1/change-requests/policies",
        headers=_auth(ta),
        json={
            "name": "x",
            "resource_type": "subnet",
            "action": "delete",
            "min_count": 2_000_000,
        },
    )
    assert r2.status_code == 422, r2.text


# ── #3: break-glass result audit survives an apply() failure ──────────────────


@pytest.mark.asyncio
async def test_break_glass_audit_survives_apply_failure(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3 HIGH regression: a break-glass whose apply() raises after the
    re-confirm + preview must STILL leave a durable result audit row (the
    success audit is committed in its own txn BEFORE apply runs). Forces the
    apply path to blow up (the disable_module apply calls set_module_enabled);
    asserts the audit row landed and the exception surfaced."""
    from app.services import feature_modules as fm_source

    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True, password="hunter2!")
    await db_session.commit()

    # apply() does a LOCAL ``from app.services.feature_modules import
    # set_module_enabled`` at call time, so patching the source-module attr
    # forces the disable_module apply branch to raise AFTER the preview + the
    # break-glass success audit was committed in its own txn.
    async def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated apply failure")

    monkeypatch.setattr(fm_source, "set_module_enabled", _boom, raising=True)

    with pytest.raises(Exception):  # noqa: B017 — the apply error propagates
        await client.post(
            "/api/v1/admin/feature-modules/break-glass",
            headers=_auth(ta),
            json={
                "kind": "disable_module",
                "password": "hunter2!",
                "confirm_phrase": "BREAK GLASS",
            },
        )

    # The HIGH-severity success audit is durable despite the apply failure.
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


# ── #4 / #2: denied break-glass uses the lower-severity action string ─────────


@pytest.mark.asyncio
async def test_break_glass_bad_password_uses_denied_action(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#4: a post-auth validation failure (bad password) records the distinct
    lower-severity ``approvals.break_glass_denied`` action, NOT the high-sev
    success action — so SIEM rules keyed on the success action stay clean."""
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
    # No high-sev success/forbidden row under the success action…
    n_success_action = (
        await db_session.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "approvals.break_glass",
            )
        )
    ).scalar_one()
    assert n_success_action == 0
    # …but a denied-action row landed.
    n_denied = (
        await db_session.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "approvals.break_glass_denied",
                AuditLog.result == "forbidden",
            )
        )
    ).scalar_one()
    assert n_denied == 1


# ── #5 / #6: concurrent break-glass leaves the queued CR resolvable ──────────


@pytest.mark.asyncio
async def test_premise_lost_resolves_cr_idempotent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#6 regression: a queued control-op change_request whose gate premise
    evaporated (the lock got turned off out of band, simulating a concurrent
    break-glass unlock) resolves on approve as IDEMPOTENT EXECUTED — not stuck
    pending / failed."""
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    _, tb = await _user(db_session, name="super-b", superadmin=True)
    await db_session.commit()

    # Queue a disable-module CR while locked.
    cr_id = (
        await client.patch(
            "/api/v1/admin/feature-modules/governance.approvals",
            headers=_auth(ta),
            json={"enabled": False},
        )
    ).json()["change_request_id"]

    # The lock gets turned off out of band (simulate a concurrent break-glass
    # unlock). The premise for the queued CR is now gone.
    await _set_lock(db_session, on=False)
    await db_session.commit()

    # A second superadmin approves → the CR resolves as idempotent executed,
    # and the module is NOT silently disabled under the now-orphaned premise.
    r = await client.post(f"/api/v1/change-requests/{cr_id}/approve", headers=_auth(tb), json={})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "executed"
    assert r.json()["result"].get("idempotent") is True
    feature_modules.invalidate_cache()
    assert await feature_modules.is_module_enabled(db_session, "governance.approvals")


@pytest.mark.asyncio
async def test_delete_policy_already_gone_resolves_idempotent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#5 regression: a queued delete_policy CR whose target was deleted out of
    band (e.g. a concurrent break-glass delete) resolves on approve as
    idempotent executed rather than failing."""
    await _enable_module(db_session)
    await _set_lock(db_session, on=True)
    p = await _policy(db_session, is_builtin=False)
    _, ta = await _user(db_session, name="super-a", superadmin=True)
    _, tb = await _user(db_session, name="super-b", superadmin=True)
    await db_session.commit()

    cr_id = (
        await client.delete(f"/api/v1/change-requests/policies/{p.id}", headers=_auth(ta))
    ).json()["change_request_id"]

    # The policy vanishes out of band before approval.
    target = await db_session.get(ApprovalPolicy, p.id)
    assert target is not None
    await db_session.delete(target)
    await db_session.commit()

    r = await client.post(f"/api/v1/change-requests/{cr_id}/approve", headers=_auth(tb), json={})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "executed"
    assert r.json()["result"].get("idempotent") is True
