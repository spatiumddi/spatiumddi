"""HTTP-level tests for the Network Discovery API.

The SNMP probe / Celery delay are mocked out — we're verifying the
router shape (auth, validation, audit, secrets-never-echoed) rather
than re-testing the poller (which has its own coverage in
``test_snmp_poller.py``).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.ipam import IPSpace
from app.models.network import NetworkDevice
from app.services.snmp.errors import SNMPAuthError, SNMPTimeoutError
from app.services.snmp.poller import SysInfo


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"admin-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@example.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"net-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


def _create_body(space_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    body = {
        "name": f"sw-{uuid.uuid4().hex[:6]}",
        "hostname": "10.0.0.1",
        "ip_address": "10.0.0.1",
        "device_type": "switch",
        "snmp_version": "v2c",
        "community": "public",
        "ip_space_id": str(space_id),
    }
    body.update(overrides)
    return body


# ── CRUD ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_device_returns_201_and_hides_secrets(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    res = await client.post(
        "/api/v1/network-devices",
        headers={"Authorization": f"Bearer {token}"},
        json=_create_body(space.id),
    )
    assert res.status_code == 201, res.text
    payload = res.json()
    assert payload["has_community"] is True
    # Plaintext community must not appear anywhere in the response.
    assert "public" not in res.text
    assert payload["last_poll_status"] == "pending"
    assert payload["ip_space_name"] == space.name


@pytest.mark.asyncio
async def test_create_device_rejects_missing_community(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    body = _create_body(space.id)
    body.pop("community")
    res = await client.post(
        "/api/v1/network-devices",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_device_v3_authpriv_requires_keys(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    body = _create_body(
        space.id,
        snmp_version="v3",
        community=None,
        v3_security_name="snmpadmin",
        v3_security_level="authPriv",
        # Intentionally omit v3_auth_protocol / key
    )
    res = await client.post(
        "/api/v1/network-devices",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_then_list_get_patch_delete_round_trip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}

    # Create
    res = await client.post("/api/v1/network-devices", headers=headers, json=_create_body(space.id))
    assert res.status_code == 201
    dev_id = res.json()["id"]

    # List — paginated envelope: {items, total, page, page_size}
    res = await client.get("/api/v1/network-devices", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"items", "total", "page", "page_size"}
    assert body["total"] >= 1
    assert any(r["id"] == dev_id for r in body["items"])

    # Get
    res = await client.get(f"/api/v1/network-devices/{dev_id}", headers=headers)
    assert res.status_code == 200
    assert res.json()["device_type"] == "switch"

    # Patch — change description, rotate community, toggle auto-create
    res = await client.patch(
        f"/api/v1/network-devices/{dev_id}",
        headers=headers,
        json={
            "description": "core switch",
            "community": "rotated",
            "auto_create_discovered": True,
        },
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["description"] == "core switch"
    assert payload["auto_create_discovered"] is True
    assert payload["has_community"] is True
    assert "rotated" not in res.text  # secret never echoed

    # Delete
    res = await client.delete(f"/api/v1/network-devices/{dev_id}", headers=headers)
    assert res.status_code == 204
    res = await client.get(f"/api/v1/network-devices/{dev_id}", headers=headers)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_create_writes_audit_row(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    res = await client.post(
        "/api/v1/network-devices",
        headers={"Authorization": f"Bearer {token}"},
        json=_create_body(space.id),
    )
    assert res.status_code == 201

    audit_rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.resource_type == "network_device")
            )
        )
        .scalars()
        .all()
    )
    assert len(list(audit_rows)) >= 1
    row = list(audit_rows)[0]
    # Secrets must not appear in the audit row's new_value blob.
    assert "community" not in (row.new_value or {})


# ── Test Connection ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_connection_returns_success_and_persists_sys_metadata(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    res = await client.post("/api/v1/network-devices", headers=headers, json=_create_body(space.id))
    dev_id = res.json()["id"]

    fake = SysInfo(
        sys_descr="Cisco IOS",
        sys_object_id="1.3.6.1.4.1.9.1.1",
        sys_name="sw1",
        sys_uptime_seconds=42,
        vendor="Cisco",
    )

    with patch(
        "app.services.snmp.poller.test_connection",
        new=AsyncMock(return_value=fake),
    ):
        res = await client.post(f"/api/v1/network-devices/{dev_id}/test", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["vendor"] == "Cisco"
    assert body["sys_name"] == "sw1"
    assert body["sys_descr"] == "Cisco IOS"


@pytest.mark.asyncio
async def test_test_connection_classifies_timeout(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    res = await client.post("/api/v1/network-devices", headers=headers, json=_create_body(space.id))
    dev_id = res.json()["id"]

    with patch(
        "app.services.snmp.poller.test_connection",
        new=AsyncMock(side_effect=SNMPTimeoutError("Request timed out")),
    ):
        res = await client.post(f"/api/v1/network-devices/{dev_id}/test", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is False
    assert body["error_kind"] == "timeout"


@pytest.mark.asyncio
async def test_test_connection_classifies_auth_failure(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    res = await client.post("/api/v1/network-devices", headers=headers, json=_create_body(space.id))
    dev_id = res.json()["id"]

    with patch(
        "app.services.snmp.poller.test_connection",
        new=AsyncMock(side_effect=SNMPAuthError("auth")),
    ):
        res = await client.post(f"/api/v1/network-devices/{dev_id}/test", headers=headers)
    assert res.json()["error_kind"] == "auth_failure"


# ── Poll Now ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_now_queues_celery_task(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    res = await client.post("/api/v1/network-devices", headers=headers, json=_create_body(space.id))
    dev_id = res.json()["id"]

    fake_result = MagicMock()
    fake_result.id = "celery-task-123"

    with patch("app.tasks.snmp_poll.poll_device.delay", return_value=fake_result) as m:
        res = await client.post(f"/api/v1/network-devices/{dev_id}/poll-now", headers=headers)
    assert res.status_code == 202
    body = res.json()
    assert body["task_id"] == "celery-task-123"
    m.assert_called_once_with(dev_id)


# ── List sub-resources ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_interfaces_arp_fdb_return_empty_initially(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    res = await client.post("/api/v1/network-devices", headers=headers, json=_create_body(space.id))
    dev_id = res.json()["id"]

    for path in ("interfaces", "arp", "fdb"):
        res = await client.get(f"/api/v1/network-devices/{dev_id}/{path}", headers=headers)
        assert res.status_code == 200, path
        body = res.json()
        assert body["items"] == [], path
        assert body["total"] == 0, path


# ── Network context (mounted on the IPAM router) ───────────────────


@pytest.mark.asyncio
async def test_network_context_returns_empty_when_ip_has_no_mac(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.models.ipam import IPAddress, IPBlock, Subnet

    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)

    block = IPBlock(space_id=space.id, network="10.0.0.0/16", name="b")
    db_session.add(block)
    await db_session.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.0.0.0/24", name="s")
    db_session.add(subnet)
    await db_session.flush()
    ip = IPAddress(subnet_id=subnet.id, address="10.0.0.5", status="allocated")
    db_session.add(ip)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    res = await client.get(f"/api/v1/ipam/addresses/{ip.id}/network-context", headers=headers)
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.asyncio
async def test_network_context_joins_fdb_to_device(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.core.crypto import encrypt_str
    from app.models.ipam import IPAddress, IPBlock, Subnet
    from app.models.network import NetworkFdbEntry, NetworkInterface

    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    block = IPBlock(space_id=space.id, network="10.0.0.0/16", name="b")
    db_session.add(block)
    await db_session.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.0.0.0/24", name="s")
    db_session.add(subnet)
    await db_session.flush()
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.0.0.99",
        status="allocated",
        mac_address="aa:bb:cc:dd:ee:99",
    )
    db_session.add(ip)
    device = NetworkDevice(
        name="sw-test",
        hostname="10.0.0.1",
        ip_address="10.0.0.1",
        snmp_version="v2c",
        ip_space_id=space.id,
        community_encrypted=encrypt_str("public"),
    )
    db_session.add(device)
    await db_session.flush()
    iface = NetworkInterface(device_id=device.id, if_index=12, name="Gi0/12", alias="vm-trunk")
    db_session.add(iface)
    await db_session.flush()
    fdb = NetworkFdbEntry(
        device_id=device.id,
        interface_id=iface.id,
        mac_address="aa:bb:cc:dd:ee:99",
        vlan_id=42,
        fdb_type="learned",
    )
    db_session.add(fdb)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    res = await client.get(f"/api/v1/ipam/addresses/{ip.id}/network-context", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["device_name"] == "sw-test"
    assert entry["interface_name"] == "Gi0/12"
    assert entry["interface_alias"] == "vm-trunk"
    assert entry["vlan_id"] == 42
    assert entry["fdb_type"] == "learned"
