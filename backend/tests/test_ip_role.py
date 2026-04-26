"""Tests for the new ``IPAddress.role`` field + role-driven collision exemption.

Covers:
  * Validator rejects unknown roles with 422.
  * Empty string normalised to None on create / update / bulk.
  * ``vrrp`` / ``vip`` / ``anycast`` rows skip the MAC-collision warning.
  * Plain hosts still hit the warning when MACs match.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPBlock, IPSpace, Subnet


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"role-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Role Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _seed_subnet(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"role-sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.50.0.0/16", name="role-blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.50.1.0/24",
        name="role-sn",
    )
    db.add(subnet)
    await db.flush()
    return subnet


@pytest.mark.asyncio
async def test_create_with_known_role_succeeds(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)

    resp = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "address": "10.50.1.10",
            "hostname": "vip-www",
            "status": "allocated",
            "role": "vip",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "vip"


@pytest.mark.asyncio
async def test_create_with_unknown_role_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)

    resp = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "address": "10.50.1.11",
            "hostname": "bad-role",
            "role": "totally-made-up",
        },
    )
    assert resp.status_code == 422
    assert "role" in resp.text.lower()


@pytest.mark.asyncio
async def test_empty_string_role_normalised_to_null(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)

    resp = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "address": "10.50.1.12",
            "hostname": "no-role",
            "role": "",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] is None


@pytest.mark.asyncio
async def test_update_role(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={
            "address": "10.50.1.13",
            "hostname": "mut-role",
            "status": "allocated",
        },
    )
    assert create.status_code == 201
    ip_id = create.json()["id"]

    upd = await client.put(
        f"/api/v1/ipam/addresses/{ip_id}",
        headers=headers,
        json={"role": "loopback"},
    )
    assert upd.status_code == 200
    assert upd.json()["role"] == "loopback"

    # Clear the role.
    clear = await client.put(
        f"/api/v1/ipam/addresses/{ip_id}",
        headers=headers,
        json={"role": None},
    )
    assert clear.status_code == 200
    assert clear.json()["role"] is None


@pytest.mark.asyncio
async def test_vrrp_role_exempts_mac_collision(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two IPs sharing a virtual MAC should be permissible without
    ``force=True`` when the second IP carries a shared role."""
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    shared_mac = "00:00:5e:00:01:01"  # canonical VRRP virtual MAC

    first = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={
            "address": "10.50.1.20",
            "hostname": "vrrp-master",
            "mac_address": shared_mac,
            "role": "vrrp",
        },
    )
    assert first.status_code == 201, first.text

    # Second IP with the same MAC + a shared role: should NOT
    # require force / surface a collision warning.
    second = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={
            "address": "10.50.1.21",
            "hostname": "vrrp-backup",
            "mac_address": shared_mac,
            "role": "vrrp",
        },
    )
    assert second.status_code == 201, second.text


@pytest.mark.asyncio
async def test_plain_host_role_still_warns(client: AsyncClient, db_session: AsyncSession) -> None:
    """Two ``host`` rows sharing a MAC should still surface the
    collision warning — the exemption is opt-in via shared roles."""
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    mac = "aa:bb:cc:dd:ee:ff"

    first = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={
            "address": "10.50.1.30",
            "hostname": "host-a",
            "mac_address": mac,
        },
    )
    assert first.status_code == 201

    # Same MAC, host role → 409 with collision warnings.
    second = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={
            "address": "10.50.1.31",
            "hostname": "host-b",
            "mac_address": mac,
            "role": "host",
        },
    )
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert any(w["kind"] == "mac_collision" for w in detail["warnings"])


@pytest.mark.asyncio
async def test_bulk_edit_role(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    ids = []
    for i in (40, 41, 42):
        r = await client.post(
            f"/api/v1/ipam/subnets/{subnet.id}/addresses",
            headers=headers,
            json={
                "address": f"10.50.1.{i}",
                "hostname": f"bulk-host-{i}",
                "status": "allocated",
            },
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])

    bulk = await client.post(
        "/api/v1/ipam/addresses/bulk-edit",
        headers=headers,
        json={"ip_ids": ids, "changes": {"role": "anycast"}},
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["updated_count"] == 3

    listing = await client.get(f"/api/v1/ipam/subnets/{subnet.id}/addresses", headers=headers)
    rows = {row["id"]: row for row in listing.json()}
    for ip_id in ids:
        assert rows[ip_id]["role"] == "anycast"
