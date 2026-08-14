"""FortiGate (agentless cloud DHCP) rides the same write-through seam as Windows.

PR #616 generalised the DHCP write-through so agentless members converge on an
explicit push (agent-based Kea drops a soft-deleted scope from the bundle; a
push driver does not). #621 wired that for Windows. This proves the seam was
extended to agentless *cloud* drivers (FortiGate): scope/static edits,
soft-delete, and restore all reach the cloud push helpers for a group with a
fortigate member — with the pending-delete row correctly excluded from the
whole-object rebuild.

The FortiOS payloads themselves are covered by ``test_fortigate_dhcp_driver.py``;
the seam-routing tests here stub the cloud push (patched in the
``windows_writethrough`` namespace, where the seam imported the names) and assert
the seam invokes it. ``test_real_upsert_persists_provider_ref`` drives the REAL
``push_cloud_scope_upsert`` end-to-end (stubbing only the driver's leaf
``_client``) so the ownership-marker persistence (#630) is actually exercised.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_dict
from app.core.security import create_access_token
from app.models.auth import User
from app.models.dhcp import DHCPScope, DHCPServer, DHCPServerGroup, DHCPStaticAssignment
from app.models.ipam import IPBlock, IPSpace, Subnet

CIDR = "10.88.0.0/24"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Minimal async-context fake of the FortiOS client, serving canned
    envelopes and recording calls (see test_fortigate_dhcp_driver.py)."""

    def __init__(self, queues: dict[str, list[Any]]) -> None:
        self._queues = queues
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def _next(self, method: str, path: str, body: Any) -> _FakeResponse:
        self.calls.append({"method": method, "path": path, "json": body})
        return self._queues[method].pop(0)

    async def get(self, path: str, params: Any = None) -> _FakeResponse:
        return self._next("get", path, None)

    async def post(self, path: str, json: Any = None) -> _FakeResponse:
        return self._next("post", path, json)

    async def put(self, path: str, json: Any = None) -> _FakeResponse:
        return self._next("put", path, json)

    async def delete(self, path: str, params: Any = None) -> _FakeResponse:
        return self._next("delete", path, None)


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


async def test_real_upsert_persists_provider_ref(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the REAL push_cloud_scope_upsert (only the leaf _client stubbed):
    a first push with no object on the interface POSTs a new DHCP server and
    records its FortiOS mkey on the scope's provider_refs (#630)."""
    from app.drivers.dhcp.fortigate import FortiGateDHCPDriver
    from app.services.dhcp.cloud_writethrough import push_cloud_scope_upsert

    _user, scope = await _setup(db_session)
    # A reservation so the rebuilt scope object carries a reserved-address.
    db_session.add(
        DHCPStaticAssignment(
            scope_id=scope.id,
            ip_address="10.88.0.10",
            mac_address="00:11:22:33:44:55",
            hostname="h",
        )
    )
    await db_session.flush()
    server = (
        await db_session.execute(
            select(DHCPServer).where(DHCPServer.server_group_id == scope.group_id)
        )
    ).scalar_one()

    fake = _FakeClient(
        {
            "get": [
                # interface list — port2's primary IP CIDR == the scope CIDR
                _FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": [{"name": "port2", "ip": ["10.88.0.1", "255.255.255.0"]}],
                    },
                ),
                # existing DHCP servers on the box — none
                _FakeResponse(200, {"status": "success", "results": []}),
            ],
            "post": [_FakeResponse(200, {"status": "success", "mkey": 9})],
        }
    )
    monkeypatch.setattr(FortiGateDHCPDriver, "_client", lambda self, server, creds: fake)

    await push_cloud_scope_upsert(db_session, scope)

    # A POST landed (create), carrying the reservation, on interface port2…
    post = next(c for c in fake.calls if c["method"] == "post")
    assert post["json"]["interface"] == "port2"
    assert post["json"]["reserved-address"][0]["ip"] == "10.88.0.10"
    # …and the FortiOS mkey is recorded as this scope+server's ownership marker.
    assert scope.provider_refs == {str(server.id): {"mkey": 9, "interface": "port2"}}


async def test_scope_create_adoption_409_then_adopt_retry(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#865 — the scope-create dead end. A create whose pre-commit push hits
    the adoption guard must 409 with the ``X-Adoption-Required`` marker (so the
    modal can tell it apart from the duplicate-scope 409) and roll the scope
    back; retrying with ``?adopt_existing=true`` threads the flag through
    ``push_scope_upsert`` to the cloud push and persists the scope."""
    from app.services.dhcp.cloud_writethrough import CloudAdoptionRequired

    user, scope = await _setup(db_session)
    subnet1 = await db_session.get(Subnet, scope.subnet_id)
    assert subnet1 is not None
    group_id = scope.group_id
    # A second subnet with no scope yet — the create target.
    subnet2 = Subnet(
        space_id=subnet1.space_id,
        block_id=subnet1.block_id,
        name="s2",
        network="10.89.0.0/24",
    )
    db_session.add(subnet2)
    await db_session.flush()
    subnet2_id = subnet2.id
    await db_session.commit()

    adopt_flags: list[bool] = []

    async def _stub(db: AsyncSession, sc: DHCPScope, **kw: object) -> None:
        adopt = bool(kw.get("adopt_existing"))
        adopt_flags.append(adopt)
        if not adopt:
            raise CloudAdoptionRequired(
                "A DHCP server already exists on FortiGate interface 'port2' "
                "that SpatiumDDI did not create."
            )

    monkeypatch.setattr("app.services.dhcp.windows_writethrough.push_cloud_scope_upsert", _stub)

    hdrs = {"Authorization": f"Bearer {create_access_token(str(user.id))}"}
    body = {"group_id": str(group_id), "name": "vlan30"}

    resp = await client.post(
        f"/api/v1/dhcp/subnets/{subnet2_id}/dhcp-scopes", json=body, headers=hdrs
    )
    assert resp.status_code == 409, resp.text
    assert resp.headers.get("x-adoption-required") == "true"
    # The create rolled back — no half-persisted scope for Force Sync to find.
    # (The test client shares this session and skips the per-request
    # ``session.close()`` the real ``get_db`` does, so discard the pending
    # flush the same way close would before checking what was persisted.)
    await db_session.rollback()
    orphan = (
        (await db_session.execute(select(DHCPScope).where(DHCPScope.subnet_id == subnet2_id)))
        .unique()
        .scalar_one_or_none()
    )
    assert orphan is None

    resp = await client.post(
        f"/api/v1/dhcp/subnets/{subnet2_id}/dhcp-scopes?adopt_existing=true",
        json=body,
        headers=hdrs,
    )
    assert resp.status_code == 201, resp.text
    assert adopt_flags == [False, True]

    # The duplicate-scope 409 must NOT carry the adoption marker — an
    # adopt-retry there would be wrong, and the modal keys on the header.
    resp = await client.post(
        f"/api/v1/dhcp/subnets/{subnet2_id}/dhcp-scopes", json=body, headers=hdrs
    )
    assert resp.status_code == 409
    assert resp.headers.get("x-adoption-required") is None


async def test_scope_update_threads_adopt_existing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#865 — the edit/activation path hits the same guard, so update accepts
    the same opt-in and forwards it to the cloud push."""
    user, scope = await _setup(db_session)
    scope_id = scope.id
    await db_session.commit()

    adopt_flags: list[bool] = []

    async def _stub(db: AsyncSession, sc: DHCPScope, **kw: object) -> None:
        adopt_flags.append(bool(kw.get("adopt_existing")))

    monkeypatch.setattr("app.services.dhcp.windows_writethrough.push_cloud_scope_upsert", _stub)

    hdrs = {"Authorization": f"Bearer {create_access_token(str(user.id))}"}
    resp = await client.put(
        f"/api/v1/dhcp/scopes/{scope_id}?adopt_existing=true",
        json={"description": "adopted"},
        headers=hdrs,
    )
    assert resp.status_code == 200, resp.text
    assert adopt_flags == [True]
