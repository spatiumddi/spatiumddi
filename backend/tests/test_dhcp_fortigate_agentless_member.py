"""FortiGate (agentless cloud DHCP) rides the same write-through seam as Windows.

PR #616 generalised the DHCP write-through so agentless members converge on an
explicit push (agent-based Kea drops a soft-deleted scope from the bundle; a
push driver does not). #621 wired that for Windows. This proves the seam was
extended to agentless *cloud* drivers (FortiGate): scope/static edits,
soft-delete, and restore all reach the cloud push helpers for a group with a
fortigate member — with the pending-delete row correctly excluded from the
whole-object rebuild.

The FortiOS payloads themselves are covered by ``test_fortigate_dhcp_driver.py``;
here we stub the cloud push (patched in the ``windows_writethrough`` namespace,
where the seam imported the names) and assert the seam invokes it.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_dict
from app.core.security import create_access_token
from app.models.auth import User
from app.models.dhcp import DHCPScope, DHCPServer, DHCPServerGroup, DHCPStaticAssignment
from app.models.ipam import IPBlock, IPSpace, Subnet

CIDR = "10.88.0.0/24"


async def _setup(db: AsyncSession) -> tuple[User, DHCPScope]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="T",
        hashed_password="x",
        is_superadmin=True,
    )
    space = IPSpace(name=f"fg-{uuid.uuid4().hex[:6]}", description="")
    db.add_all([user, space])
    await db.flush()
    block = IPBlock(space_id=space.id, name="b", network=CIDR)
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, name="s", network=CIDR)
    group = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, group])
    await db.flush()
    server = DHCPServer(
        name="fw",
        driver="fortigate",
        host="10.0.0.1",
        port=443,
        server_group_id=group.id,
        credentials_encrypted=encrypt_dict({"api_token": "t", "vdom": "root", "verify_tls": False}),
    )
    scope = DHCPScope(group_id=group.id, subnet_id=subnet.id, name="sc", is_active=True)
    db.add_all([server, scope])
    await db.flush()
    return user, scope


async def test_scope_upsert_reaches_cloud_member(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.dhcp.windows_writethrough import push_scope_upsert

    _user, scope = await _setup(db_session)
    calls: list[uuid.UUID] = []

    async def _stub(db: AsyncSession, sc: DHCPScope, **kw: object) -> None:
        calls.append(sc.id)

    monkeypatch.setattr("app.services.dhcp.windows_writethrough.push_cloud_scope_upsert", _stub)
    await push_scope_upsert(db_session, scope)
    assert calls == [scope.id]


async def test_soft_delete_reaches_cloud_member(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.ai.operations_risky import DeleteScopeArgs, _apply_delete_scope

    user, scope = await _setup(db_session)
    scope_id = scope.id
    removed: list[uuid.UUID] = []

    async def _stub(db: AsyncSession, sc: DHCPScope, **kw: object) -> None:
        removed.append(sc.id)

    # The #616 soft path routes through _push_agentless_scope_deletes ->
    # push_scope_delete -> push_cloud_scope_delete.
    monkeypatch.setattr("app.services.dhcp.windows_writethrough.push_cloud_scope_delete", _stub)
    await _apply_delete_scope(db_session, user, DeleteScopeArgs(scope_id=scope_id, permanent=False))
    assert removed == [scope_id], "soft-delete must remove the scope from cloud members"


async def test_static_delete_excludes_pending_row_for_cloud(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.dhcp.windows_writethrough import push_static_change

    _user, scope = await _setup(db_session)
    st = DHCPStaticAssignment(
        scope_id=scope.id,
        ip_address="10.88.0.10",
        mac_address="00:11:22:33:44:55",
        hostname="h",
    )
    db_session.add(st)
    await db_session.flush()

    seen: list[object] = []

    async def _stub(
        db: AsyncSession, sc: DHCPScope, *, exclude_pool_ids=None, exclude_static_ids=None
    ) -> None:
        seen.append(exclude_static_ids)

    monkeypatch.setattr("app.services.dhcp.windows_writethrough.push_cloud_scope_upsert", _stub)
    # A delete pushes the whole scope object rebuilt WITHOUT the doomed row (the
    # endpoint hard-deletes the static only after this call).
    await push_static_change(db_session, st, action="delete")
    assert seen == [{st.id}]


async def test_restore_reattaches_cloud_member(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.ai.operations_risky import DeleteScopeArgs, _apply_delete_scope

    user, scope = await _setup(db_session)
    scope_id = scope.id
    token = create_access_token(str(user.id))

    async def _noop_delete(db: AsyncSession, sc: DHCPScope, **kw: object) -> None:
        return None

    upserts: list[uuid.UUID] = []

    async def _stub_upsert(db: AsyncSession, sc: DHCPScope, **kw: object) -> None:
        upserts.append(sc.id)

    monkeypatch.setattr(
        "app.services.dhcp.windows_writethrough.push_cloud_scope_delete", _noop_delete
    )
    monkeypatch.setattr(
        "app.services.dhcp.windows_writethrough.push_cloud_scope_upsert", _stub_upsert
    )

    await _apply_delete_scope(db_session, user, DeleteScopeArgs(scope_id=scope_id, permanent=False))
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/admin/trash/dhcp_scope/{scope_id}/restore",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    # push_scope_restore re-pushes the whole object to cloud members.
    assert scope_id in upserts
