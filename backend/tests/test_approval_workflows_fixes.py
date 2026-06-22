"""Regression tests for the #62 code-review fixes (inline-fidelity contract).

These pin the seven findings the review surfaced on top of the original
two-person approval workflow (covered by ``test_approval_workflows.py``):

* **#7 — ValueError→500.** A module-OFF DELETE of a *missing* subnet must
  surface the original handler's ``404`` (the factored ``apply()`` used to
  raise a bare ``ValueError`` → 500).
* **#7/#17 — not-empty 409.** A module-OFF permanent DELETE of a *non-empty*
  subnet without ``force`` must surface ``409`` (not 500), exactly like the
  pre-#62 handler.
* **#1 — no inline permission re-check.** A delegate admitted by the IPAM
  router's coarse gate via a scoped ``{delete, address_set, <id>}`` grant
  (#103) must be able to soft-delete inline WITHOUT the factored ``apply()``
  403/500-ing on a permission re-check it shouldn't run.
* **#8 — approve-path superadmin parity.** ``delete_zone`` / ``delete_scope``
  / ``delete_group`` were fully ``SuperAdmin``-gated at the route, so their
  ``apply()`` must require superadmin ALWAYS — the approve path (which
  bypasses the route's dependency) relies on this.
* **#5 — fail-closed on a deleted requester.** When ``requested_by_user_id``
  is NULL (requester deleted), approval is REFUSED (409), not fail-open.
* **#4 — non-gateable policy rejected.** Creating an approval policy with an
  action that isn't wired to a registered risky op → ``422``.
* **#10 — single-namespaced event type.** The ``change_request.approved``
  event type derives once (no ``change_request.change_request.*``), and the
  webhook catalog advertises the real lifecycle events.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import Group, Role, User
from app.models.change_request import ChangeRequest
from app.models.feature_module import FeatureModule
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services import feature_modules

# ── Fixtures (mirrors test_approval_workflows.py) ──────────────────────────────


async def _user(
    db: AsyncSession,
    *,
    name: str,
    permissions: list[dict] | None = None,
    superadmin: bool = False,
) -> tuple[User, str]:
    user = User(
        username=f"{name}-{uuid.uuid4().hex[:8]}",
        email=f"{name}-{uuid.uuid4().hex[:8]}@t.io",
        display_name=name,
        hashed_password=hash_password("password123"),
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


async def _subnet(db: AsyncSession, network: str = "10.9.5.0/24") -> Subnet:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.9.0.0/16", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=network, name="s")
    db.add(subnet)
    await db.flush()
    return subnet


async def _enable_module(db: AsyncSession, *, enabled: bool = True) -> None:
    existing = await db.get(FeatureModule, "governance.approvals")
    if existing is None:
        db.add(FeatureModule(id="governance.approvals", enabled=enabled))
    else:
        existing.enabled = enabled
    await db.flush()
    feature_modules.invalidate_cache()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── #7: missing subnet → 404 (not 500) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_module_off_delete_missing_subnet_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Module-OFF DELETE of a non-existent subnet surfaces 404, not a 500 from
    a bare ValueError in the factored apply()."""
    await _enable_module(db_session, enabled=False)
    _, token = await _user(
        db_session,
        name="op",
        permissions=[{"action": "delete", "resource_type": "subnet"}],
    )
    r = await client.delete(f"/api/v1/ipam/subnets/{uuid.uuid4()}", headers=_auth(token))
    assert r.status_code == 404, r.text


# ── #7/#17: non-empty permanent delete without force → 409 ─────────────────────


@pytest.mark.asyncio
async def test_module_off_delete_nonempty_permanent_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Module-OFF permanent DELETE of a non-empty subnet without force surfaces
    409 (the inline handler's not-empty conflict), not a 500."""
    await _enable_module(db_session, enabled=False)
    subnet = await _subnet(db_session)
    db_session.add(IPAddress(subnet_id=subnet.id, address="10.9.5.42", status="allocated"))
    await db_session.flush()
    # Permanent path requires superadmin; use one so we exercise the 409 branch.
    _, token = await _user(db_session, name="sa", superadmin=True)

    r = await client.delete(
        f"/api/v1/ipam/subnets/{subnet.id}?permanent=true", headers=_auth(token)
    )
    assert r.status_code == 409, r.text
    assert "not empty" in r.json()["detail"].lower()
    # Untouched.
    await db_session.refresh(subnet)
    assert subnet.deleted_at is None


# ── #1: scoped address-set delegate deletes inline without a perm re-check ─────


@pytest.mark.asyncio
async def test_scoped_delegate_inline_delete_no_500(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A delegate the IPAM router gate admits via a scoped {delete,
    address_set, <id>} grant (#103) can soft-delete inline — the factored
    apply() must NOT run enforce_operation_permission (which would 403/500 a
    delegate lacking the type-level {delete, subnet})."""
    await _enable_module(db_session, enabled=False)
    subnet = await _subnet(db_session)
    # ONLY an instance-scoped address_set delete grant — no {delete, subnet}.
    _, token = await _user(
        db_session,
        name="delegate",
        permissions=[
            {
                "action": "delete",
                "resource_type": "address_set",
                "resource_id": str(uuid.uuid4()),
            }
        ],
    )

    r = await client.delete(f"/api/v1/ipam/subnets/{subnet.id}", headers=_auth(token))
    # Must not 500 (perm re-check) and must not 403 — the coarse gate admitted
    # the delegate and the inline soft-delete runs.
    assert r.status_code == 204, r.text
    await db_session.refresh(subnet)
    assert subnet.deleted_at is not None


# ── #8: apply() requires superadmin ALWAYS for zone / scope / group ────────────


@pytest.mark.asyncio
async def test_delete_zone_apply_requires_superadmin(db_session: AsyncSession) -> None:
    """delete_zone.apply() raises 403 for a non-superadmin — the route was
    fully SuperAdmin-gated, so the approve path (which bypasses the route's
    SuperAdmin dependency) must enforce it inside apply() (#8). The superadmin
    check runs before the zone lookup, so random ids suffice."""
    from fastapi import HTTPException

    from app.services.ai.operations_risky import DeleteZoneArgs, _apply_delete_zone

    nonadmin, _ = await _user(
        db_session,
        name="zone-editor",
        permissions=[{"action": "delete", "resource_type": "dns_zone"}],
    )
    args = DeleteZoneArgs(group_id=uuid.uuid4(), zone_id=uuid.uuid4(), permanent=False)
    with pytest.raises(HTTPException) as ei:
        await _apply_delete_zone(db_session, nonadmin, args)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_scope_apply_requires_superadmin(db_session: AsyncSession) -> None:
    """delete_scope.apply() requires superadmin always (#8)."""
    from fastapi import HTTPException

    from app.services.ai.operations_risky import DeleteScopeArgs, _apply_delete_scope

    nonadmin, _ = await _user(
        db_session,
        name="scope-editor",
        permissions=[{"action": "delete", "resource_type": "dhcp_scope"}],
    )
    # Missing scope would 404 — but superadmin is checked first, so a
    # non-superadmin gets 403 before the lookup.
    args = DeleteScopeArgs(scope_id=uuid.uuid4(), permanent=False)
    with pytest.raises(HTTPException) as ei:
        await _apply_delete_scope(db_session, nonadmin, args)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_group_apply_requires_superadmin(db_session: AsyncSession) -> None:
    """delete_group.apply() requires superadmin always (#8)."""
    from fastapi import HTTPException

    from app.services.ai.operations_risky import DeleteGroupArgs, _apply_delete_group

    nonadmin, _ = await _user(
        db_session,
        name="group-editor",
        permissions=[{"action": "delete", "resource_type": "dhcp_server_group"}],
    )
    args = DeleteGroupArgs(group_id=uuid.uuid4())
    with pytest.raises(HTTPException) as ei:
        await _apply_delete_group(db_session, nonadmin, args)
    assert ei.value.status_code == 403


# ── #5: approval refused when the requester was deleted (requested_by NULL) ────


@pytest.mark.asyncio
async def test_approve_refused_when_requester_deleted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A pending request whose requested_by_user_id is NULL (requester deleted)
    is refused on approve (409) — fail CLOSED, not fail open."""
    await _enable_module(db_session)
    subnet = await _subnet(db_session)
    cr = ChangeRequest(
        operation="delete_subnet",
        resource_type="subnet",
        resource_id=str(subnet.id),
        resource_display="s",
        args={"subnet_id": str(subnet.id), "force": False, "permanent": False},
        preview_text="Soft-delete subnet",
        risk_reason="delete:subnet",
        state="pending",
        requested_by_user_id=None,  # requester deleted → SET NULL
        requested_by_display="gone",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(cr)
    await db_session.flush()
    await db_session.commit()

    _, atoken = await _user(
        db_session,
        name="approver",
        permissions=[
            {"action": "approve", "resource_type": "change_request"},
            {"action": "read", "resource_type": "change_request"},
            {"action": "delete", "resource_type": "subnet"},
        ],
    )
    r = await client.post(
        f"/api/v1/change-requests/{cr.id}/approve", headers=_auth(atoken), json={}
    )
    assert r.status_code == 409, r.text
    assert "no longer exists" in r.json()["detail"].lower()

    refreshed = await db_session.get(ChangeRequest, cr.id)
    assert refreshed is not None and refreshed.state == "pending"
    await db_session.refresh(subnet)
    assert subnet.deleted_at is None


# ── #4: policy create with a non-gateable action → 422 ─────────────────────────


@pytest.mark.asyncio
async def test_policy_create_nongateable_action_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An approval policy for an action that isn't wired to a registered risky
    op (e.g. bulk_delete in P1) is rejected 422 — no enabled-but-inert rules."""
    await _enable_module(db_session)
    _, token = await _user(db_session, name="super", superadmin=True)

    r = await client.post(
        "/api/v1/change-requests/policies",
        headers=_auth(token),
        json={
            "name": "Bulk delete",
            "resource_type": "subnet",
            "action": "bulk_delete",
            "enabled": True,
        },
    )
    assert r.status_code == 422, r.text
    assert "not gateable" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_policy_create_nongateable_resource_type_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A delete policy against a non-gateable resource_type (e.g. the '*'
    wildcard, P2 only) is rejected 422."""
    await _enable_module(db_session)
    _, token = await _user(db_session, name="super2", superadmin=True)

    r = await client.post(
        "/api/v1/change-requests/policies",
        headers=_auth(token),
        json={"name": "Wildcard delete", "resource_type": "*", "action": "delete"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_policy_create_gateable_ok(client: AsyncClient, db_session: AsyncSession) -> None:
    """A delete:subnet policy (gateable) is accepted (201) — the validator
    rejects only the un-wired surface."""
    await _enable_module(db_session)
    _, token = await _user(db_session, name="super3", superadmin=True)

    r = await client.post(
        "/api/v1/change-requests/policies",
        headers=_auth(token),
        json={"name": "Delete subnet (custom)", "resource_type": "subnet", "action": "delete"},
    )
    assert r.status_code == 201, r.text


# ── #10: event type single-namespaced + catalog advertises lifecycle ──────────


def test_change_request_event_type_single_namespaced() -> None:
    """change_request.approved derives once — no change_request.change_request.*
    doubling — and the webhook catalog advertises the real lifecycle events,
    not the never-fired created/updated/deleted trio."""
    from app.services.event_publisher import (
        _RESOURCE_NAMESPACE,
        _SPECIAL_EVENT_MAP,
        _VERB_MAP,
        _audit_to_event_type,
    )

    for verb in ("requested", "approved", "rejected", "cancelled", "executed", "failed", "expired"):
        assert _audit_to_event_type(verb, "change_request") == f"change_request.{verb}"

    # Reproduce the catalog enumeration (webhooks router) and assert shape.
    catalog: set[str] = set()
    for ns in set(_RESOURCE_NAMESPACE.values()):
        for v in _VERB_MAP.values():
            catalog.add(f"{ns}.{v}")
    catalog.update(_SPECIAL_EVENT_MAP.values())

    assert not any("change_request.change_request" in t for t in catalog)
    assert "change_request.approved" in catalog
    assert "change_request.requested" in catalog
    assert "change_request.created" not in catalog
    assert "change_request.deleted" not in catalog
