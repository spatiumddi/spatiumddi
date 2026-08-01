"""#774 — ``enabled`` must not silently shadow ``is_active`` on a scope write.

``ScopeResponse`` emits only ``enabled``; ``ScopeCreate`` / ``ScopeUpdate``
accept ``is_active`` as well, because that is the column name and therefore the
one a script reaches for. The natural read-modify-write — GET, edit, PUT the
whole representation back — therefore produces a body carrying BOTH, and the
handler used to let the ``enabled`` the GET supplied overwrite the ``is_active``
the caller deliberately set. 200 OK, scope unchanged, no warning: a
"deactivate this scope" that left the scope handing out addresses.

These tests pin the resolution: a disagreement is a 422, either name works on
its own, and agreeing values still apply.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.dhcp import DHCPScope, DHCPServerGroup
from app.models.ipam import IPBlock, IPSpace, Subnet

CIDR = "192.0.2.0/24"


async def _make_token(db: AsyncSession) -> str:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _subnet_and_group(db: AsyncSession) -> tuple[Subnet, DHCPServerGroup]:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=CIDR, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=CIDR, name="s")
    grp = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, grp])
    await db.flush()
    return subnet, grp


async def _setup(client: AsyncClient, db: AsyncSession, **extra: object) -> tuple[dict, dict]:
    """Returns (auth headers, created-scope body)."""
    token = await _make_token(db)
    subnet, grp = await _subnet_and_group(db)
    await db.commit()
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        f"/api/v1/dhcp/subnets/{subnet.id}/dhcp-scopes",
        headers=h,
        json={"group_id": str(grp.id), "name": "s", **extra},
    )
    assert r.status_code in (200, 201), r.text
    return h, r.json()


@pytest.mark.asyncio
async def test_read_modify_write_with_is_active_is_refused_not_ignored(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The exact repro from the issue: GET, flip is_active, PUT it back."""
    h, scope = await _setup(client, db_session)
    assert scope["enabled"] is True

    current = (await client.get(f"/api/v1/dhcp/scopes/{scope['id']}", headers=h)).json()
    assert "is_active" not in current  # the response only ever carries `enabled`
    current["is_active"] = False  # ...so the GET's `enabled: true` is now residue

    r = await client.put(f"/api/v1/dhcp/scopes/{scope['id']}", headers=h, json=current)
    assert r.status_code == 422, r.text
    assert "is_active" in r.text and "enabled" in r.text

    # And the scope is genuinely untouched — the old behaviour answered 200
    # while doing exactly this.
    after = (await client.get(f"/api/v1/dhcp/scopes/{scope['id']}", headers=h)).json()
    assert after["enabled"] is True


@pytest.mark.asyncio
async def test_is_active_alone_deactivates(client: AsyncClient, db_session: AsyncSession) -> None:
    h, scope = await _setup(client, db_session)
    r = await client.put(f"/api/v1/dhcp/scopes/{scope['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False


@pytest.mark.asyncio
async def test_enabled_alone_deactivates(client: AsyncClient, db_session: AsyncSession) -> None:
    """What the frontend sends — must keep working untouched."""
    h, scope = await _setup(client, db_session)
    r = await client.put(f"/api/v1/dhcp/scopes/{scope['id']}", headers=h, json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False


@pytest.mark.asyncio
async def test_both_names_agreeing_is_applied(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Only a *disagreement* is refused — a client that edits both is fine."""
    h, scope = await _setup(client, db_session)
    r = await client.put(
        f"/api/v1/dhcp/scopes/{scope['id']}",
        headers=h,
        json={"enabled": False, "is_active": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False


@pytest.mark.asyncio
async def test_null_alias_counts_as_omitted(client: AsyncClient, db_session: AsyncSession) -> None:
    """``enabled: null`` means "not specified", not "disagrees with False"."""
    h, scope = await _setup(client, db_session)
    r = await client.put(
        f"/api/v1/dhcp/scopes/{scope['id']}",
        headers=h,
        json={"is_active": False, "enabled": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False


@pytest.mark.asyncio
async def test_hostname_sync_mode_alias_conflict_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The same shape on the other dual-named field found by the sweep.

    ``ScopeResponse`` emits ``hostname_sync_mode``; ``hostname_to_ipam_sync`` is
    the column name. Editing the column name on a fetched body used to be
    shadowed exactly the same way — silently, though not service-affecting.
    """
    h, scope = await _setup(client, db_session)
    r = await client.put(
        f"/api/v1/dhcp/scopes/{scope['id']}",
        headers=h,
        json={"hostname_sync_mode": "on_static_only", "hostname_to_ipam_sync": "on_lease"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_hostname_sync_mode_legacy_alias_value_agrees(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``learned`` is the legacy spelling of ``on_lease`` — same value, not a
    conflict. The comparison normalises before deciding."""
    h, scope = await _setup(client, db_session)
    r = await client.put(
        f"/api/v1/dhcp/scopes/{scope['id']}",
        headers=h,
        json={"hostname_sync_mode": "learned", "hostname_to_ipam_sync": "on_lease"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["hostname_sync_mode"] == "on_lease"


@pytest.mark.asyncio
async def test_empty_string_alias_defers_to_the_other_name(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An empty ``hostname_sync_mode`` is "not supplied", not a disagreement.

    The create handler resolves these with ``a or b``, so an empty string has
    always fallen through to the other name; the guard must not turn that into
    a 422.
    """
    token = await _make_token(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        f"/api/v1/dhcp/subnets/{subnet.id}/dhcp-scopes",
        headers=h,
        json={
            "group_id": str(grp.id),
            "name": "s",
            "hostname_sync_mode": "",
            "hostname_to_ipam_sync": "on_lease",
        },
    )
    assert r.status_code in (200, 201), r.text
    assert r.json()["hostname_sync_mode"] == "on_lease"


@pytest.mark.asyncio
async def test_refused_update_writes_no_audit_row_and_no_push(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A 422 mutates nothing, so there is nothing to audit and nothing to push.

    The guard lives on the request model, so it fires during body parsing —
    the handler, its audit write and the agent write-through never run.
    """
    h, scope = await _setup(client, db_session)
    before = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.resource_id == scope["id"])
        )
    ).scalar_one()

    r = await client.put(
        f"/api/v1/dhcp/scopes/{scope['id']}",
        headers=h,
        json={"enabled": True, "is_active": False},
    )
    assert r.status_code == 422, r.text

    after = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.resource_id == scope["id"])
        )
    ).scalar_one()
    assert after == before
    row = await db_session.get(DHCPScope, uuid.UUID(scope["id"]))
    assert row is not None
    await db_session.refresh(row)
    assert row.is_active is True
    assert row.last_pushed_at is None


@pytest.mark.asyncio
async def test_is_active_alone_is_audited_as_the_column_it_writes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The handler rewrites the alias before auditing, so the trail names the
    column that actually changed — and now it is the value the caller meant."""
    h, scope = await _setup(client, db_session)
    r = await client.put(f"/api/v1/dhcp/scopes/{scope['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 200, r.text

    audit = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.resource_id == scope["id"], AuditLog.action == "update")
            .order_by(AuditLog.timestamp.desc())
            .limit(1)
        )
    ).scalar_one()
    assert "is_active" in (audit.changed_fields or [])
    row = await db_session.get(DHCPScope, uuid.UUID(scope["id"]))
    assert row is not None
    await db_session.refresh(row)
    assert row.is_active is False


@pytest.mark.asyncio
async def test_create_refuses_conflicting_names(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _make_token(db_session)
    subnet, grp = await _subnet_and_group(db_session)
    await db_session.commit()
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        f"/api/v1/dhcp/subnets/{subnet.id}/dhcp-scopes",
        headers=h,
        json={"group_id": str(grp.id), "name": "s", "enabled": True, "is_active": False},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_create_with_is_active_alone_lands_inactive(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _h, scope = await _setup(client, db_session, is_active=False)
    assert scope["enabled"] is False
