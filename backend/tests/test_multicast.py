"""Multicast group registry tests — issue #126 Phase 1.

Covers the registry CRUD surface + the multicast-class address
validation + the feature-module gate. Membership tests do a
direct model insert for the IPAddress prerequisite to avoid
plumbing a full IPSpace → IPBlock → Subnet → IPAddress chain
through the IPAM API in every test.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.multicast import MulticastGroup


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"mc-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Multicast Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_space(db: AsyncSession, name: str | None = None) -> IPSpace:
    space = IPSpace(name=name or f"mc-space-{uuid.uuid4().hex[:8]}")
    db.add(space)
    await db.flush()
    return space


# ── Group CRUD ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_group_v4_inside_multicast_range(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "address": "239.5.7.42",
            "name": "Cam7 Studio-B HD",
            "application": "SMPTE 2110-20 video",
            "rtp_payload_type": 96,
            "bandwidth_mbps_estimate": "1485.000",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["address"] == "239.5.7.42"
    assert body["application"] == "SMPTE 2110-20 video"
    assert body["rtp_payload_type"] == 96


@pytest.mark.asyncio
async def test_create_group_v6_inside_multicast_range(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "address": "ff05::1:3",
            "name": "site-local-DHCP-relay",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["address"] == "ff05::1:3"


@pytest.mark.asyncio
async def test_create_group_rejects_unicast_address(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(space.id),
            "address": "10.0.0.5",
            "name": "should-fail",
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert any("224.0.0.0/4" in str(item) for item in detail)


@pytest.mark.asyncio
async def test_create_group_rejects_unknown_space(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "space_id": str(uuid.uuid4()),
            "address": "239.1.2.3",
            "name": "stray",
        },
    )
    assert resp.status_code == 422
    assert "space_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_groups_filters_by_space_and_search(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space_a = await _make_space(db_session, "mc-A")
    space_b = await _make_space(db_session, "mc-B")
    db_session.add_all(
        [
            MulticastGroup(
                space_id=space_a.id, address="239.1.1.1", name="cam1", application="video"
            ),
            MulticastGroup(
                space_id=space_a.id, address="239.1.1.2", name="cam2", application="audio"
            ),
            MulticastGroup(
                space_id=space_b.id, address="239.9.9.9", name="other", application="ndi"
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/multicast/groups?space_id={space_a.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {"cam1", "cam2"}

    # Substring search also looks at application.
    resp = await client.get(
        f"/api/v1/multicast/groups?space_id={space_a.id}&search=audio",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "cam2"


@pytest.mark.asyncio
async def test_update_and_delete_group(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={
            "space_id": str(space.id),
            "address": "239.5.5.5",
            "name": "before",
        },
    )
    group_id = resp.json()["id"]

    resp = await client.put(
        f"/api/v1/multicast/groups/{group_id}",
        headers=headers,
        json={"name": "after", "application": "trade-feed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "after"
    assert body["application"] == "trade-feed"

    resp = await client.delete(f"/api/v1/multicast/groups/{group_id}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get(f"/api/v1/multicast/groups/{group_id}", headers=headers)
    assert resp.status_code == 404


# ── Ports ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_port_crud_and_range_validation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={"space_id": str(space.id), "address": "239.6.6.6", "name": "ports"},
    )
    group_id = resp.json()["id"]

    # Single port (port_end null).
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/ports",
        headers=headers,
        json={"port_start": 5000, "transport": "rtp"},
    )
    assert resp.status_code == 201
    port_id = resp.json()["id"]

    # Range.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/ports",
        headers=headers,
        json={"port_start": 5004, "port_end": 5008, "transport": "rtp"},
    )
    assert resp.status_code == 201

    # port_end < port_start rejected at the schema layer.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/ports",
        headers=headers,
        json={"port_start": 6000, "port_end": 5999},
    )
    assert resp.status_code == 422

    # Invalid transport rejected.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/ports",
        headers=headers,
        json={"port_start": 7000, "transport": "bogus"},
    )
    assert resp.status_code == 422

    resp = await client.get(f"/api/v1/multicast/groups/{group_id}/ports", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = await client.delete(f"/api/v1/multicast/ports/{port_id}", headers=headers)
    assert resp.status_code == 204


# ── Memberships ───────────────────────────────────────────────────────


async def _make_ip(db: AsyncSession, space: IPSpace, addr: str) -> IPAddress:
    """Build the minimum IPSpace → IPBlock → Subnet → IPAddress chain
    so a membership test can attach a real IP. Cheaper than going
    through the IPAM API for every test."""
    block = IPBlock(space_id=space.id, name="b", network="10.0.0.0/16")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, name="s", network="10.0.0.0/24")
    db.add(subnet)
    await db.flush()
    ip = IPAddress(subnet_id=subnet.id, address=addr, status="allocated")
    db.add(ip)
    await db.flush()
    return ip


@pytest.mark.asyncio
async def test_membership_add_and_unique_triplet(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    ip = await _make_ip(db_session, space, "10.0.0.5")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={"space_id": str(space.id), "address": "239.7.7.7", "name": "memb"},
    )
    group_id = resp.json()["id"]

    # First add succeeds.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(ip.id), "role": "producer"},
    )
    assert resp.status_code == 201, resp.text
    membership_id = resp.json()["id"]

    # Same (group, ip, role) → 409.
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(ip.id), "role": "producer"},
    )
    assert resp.status_code == 409

    # Different role on same (group, ip) → succeeds (RP + producer
    # is a real configuration).
    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(ip.id), "role": "rendezvous_point"},
    )
    assert resp.status_code == 201

    resp = await client.get(f"/api/v1/multicast/groups/{group_id}/memberships", headers=headers)
    assert len(resp.json()) == 2

    resp = await client.delete(f"/api/v1/multicast/memberships/{membership_id}", headers=headers)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_membership_rejects_unknown_ip(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={"space_id": str(space.id), "address": "239.8.8.8", "name": "x"},
    )
    group_id = resp.json()["id"]

    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_membership_rejects_invalid_role(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    space = await _make_space(db_session)
    ip = await _make_ip(db_session, space, "10.0.0.6")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/multicast/groups",
        headers=headers,
        json={"space_id": str(space.id), "address": "239.4.4.4", "name": "r"},
    )
    group_id = resp.json()["id"]

    resp = await client.post(
        f"/api/v1/multicast/groups/{group_id}/memberships",
        headers=headers,
        json={"ip_address_id": str(ip.id), "role": "bogus"},
    )
    assert resp.status_code == 422


# ── Feature-module gate ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_module_returns_404(client: AsyncClient, db_session: AsyncSession) -> None:
    """When the operator turns ``network.multicast`` off the entire
    surface 404s — same shape as a not-installed plugin would behave
    in NetBox / Grafana."""
    _, token = await _make_admin(db_session)

    # Toggle the module off (default is enabled). Bypass the cache
    # since we're poking the row directly.
    await db_session.execute(
        text(
            "INSERT INTO feature_module (id, enabled) VALUES (:id, false) "
            "ON CONFLICT (id) DO UPDATE SET enabled = false"
        ).bindparams(id="network.multicast")
    )
    await db_session.commit()

    from app.services.feature_modules import invalidate_cache

    invalidate_cache()
    try:
        resp = await client.get(
            "/api/v1/multicast/groups",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404
    finally:
        # Re-enable for any subsequent tests in the session.
        await db_session.execute(
            text("UPDATE feature_module SET enabled = true WHERE id = :id").bindparams(
                id="network.multicast"
            )
        )
        await db_session.commit()
        invalidate_cache()
