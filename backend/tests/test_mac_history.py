"""Tests for the per-IP MAC history surface.

Exercises:
  * Initial MAC create writes one history row.
  * MAC change leaves the prior row intact (with its earlier
    ``last_seen``) and creates a new row.
  * Re-asserting the same MAC bumps ``last_seen`` without
    inserting a duplicate row.
  * The HTTP endpoint returns rows newest-first.
  * Cascade-delete on the parent IP wipes the history.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.ipam import IPAddress, IPBlock, IpMacHistory, IPSpace, Subnet


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"mac-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="MAC Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _seed_subnet(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"mh-sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.70.0.0/16", name="mh-blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.70.1.0/24",
        name="mh-sn",
    )
    db.add(subnet)
    await db.flush()
    return subnet


@pytest.mark.asyncio
async def test_initial_mac_creates_history_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)

    resp = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "address": "10.70.1.10",
            "hostname": "first-mac",
            "mac_address": "aa:bb:cc:00:00:01",
        },
    )
    assert resp.status_code == 201, resp.text
    ip_id = resp.json()["id"]

    rows = list(
        (await db_session.execute(select(IpMacHistory).where(IpMacHistory.ip_address_id == ip_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert str(rows[0].mac_address) == "aa:bb:cc:00:00:01"


@pytest.mark.asyncio
async def test_mac_change_keeps_old_and_appends_new(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={
            "address": "10.70.1.11",
            "hostname": "swap-mac",
            "mac_address": "aa:bb:cc:00:00:02",
        },
    )
    assert create.status_code == 201
    ip_id = create.json()["id"]

    upd = await client.put(
        f"/api/v1/ipam/addresses/{ip_id}",
        headers=headers,
        json={"mac_address": "aa:bb:cc:00:00:03"},
    )
    assert upd.status_code == 200, upd.text

    rows = list(
        (
            await db_session.execute(
                select(IpMacHistory)
                .where(IpMacHistory.ip_address_id == ip_id)
                .order_by(IpMacHistory.first_seen.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {str(r.mac_address) for r in rows} == {
        "aa:bb:cc:00:00:02",
        "aa:bb:cc:00:00:03",
    }


@pytest.mark.asyncio
async def test_repeating_same_mac_bumps_last_seen(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={
            "address": "10.70.1.12",
            "hostname": "stable-mac",
            "mac_address": "aa:bb:cc:00:00:04",
        },
    )
    assert create.status_code == 201
    ip_id = create.json()["id"]

    # Pull initial last_seen.
    initial = (
        await db_session.execute(select(IpMacHistory).where(IpMacHistory.ip_address_id == ip_id))
    ).scalar_one()
    first_last_seen = initial.last_seen

    # Touch a non-MAC field; the update path still bumps last_seen
    # because the row carries a MAC.
    upd = await client.put(
        f"/api/v1/ipam/addresses/{ip_id}",
        headers=headers,
        json={"description": "trigger update"},
    )
    assert upd.status_code == 200, upd.text

    # Force a fresh read.
    db_session.expire_all()
    rows = list(
        (await db_session.execute(select(IpMacHistory).where(IpMacHistory.ip_address_id == ip_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].last_seen >= first_last_seen


@pytest.mark.asyncio
async def test_endpoint_returns_history_newest_first(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={
            "address": "10.70.1.13",
            "hostname": "many-macs",
            "mac_address": "aa:bb:cc:00:00:05",
        },
    )
    assert create.status_code == 201
    ip_id = create.json()["id"]

    # Two more MAC changes.
    for mac in ("aa:bb:cc:00:00:06", "aa:bb:cc:00:00:07"):
        u = await client.put(
            f"/api/v1/ipam/addresses/{ip_id}",
            headers=headers,
            json={"mac_address": mac},
        )
        assert u.status_code == 200, u.text

    resp = await client.get(f"/api/v1/ipam/addresses/{ip_id}/mac-history", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 3
    seen = [row["last_seen"] for row in body]
    assert seen == sorted(seen, reverse=True), "history should be newest-first"


@pytest.mark.asyncio
async def test_endpoint_returns_404_for_missing_ip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    bogus = uuid.uuid4()
    resp = await client.get(
        f"/api/v1/ipam/addresses/{bogus}/mac-history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_history_cascades_on_ip_purge(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers=headers,
        json={
            "address": "10.70.1.14",
            "hostname": "doomed",
            "mac_address": "aa:bb:cc:00:00:08",
        },
    )
    assert create.status_code == 201
    ip_id = create.json()["id"]

    # Hard delete via the API (?permanent=true).
    delete = await client.delete(f"/api/v1/ipam/addresses/{ip_id}?permanent=true", headers=headers)
    assert delete.status_code in (200, 204), delete.text

    rows = list(
        (await db_session.execute(select(IpMacHistory).where(IpMacHistory.ip_address_id == ip_id)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_ip_without_mac_writes_no_history(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    subnet = await _seed_subnet(db_session)

    resp = await client.post(
        f"/api/v1/ipam/subnets/{subnet.id}/addresses",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "address": "10.70.1.15",
            "hostname": "no-mac-host",
        },
    )
    assert resp.status_code == 201
    ip_id = resp.json()["id"]

    rows = list(
        (await db_session.execute(select(IpMacHistory).where(IpMacHistory.ip_address_id == ip_id)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_history_endpoint_lists_distinct_macs_unique_constraint(
    db_session: AsyncSession,
) -> None:
    """Direct unit-level assertion that the unique constraint
    enforces one row per (ip, mac)."""
    from app.api.v1.ipam.router import _record_mac_history

    space = IPSpace(name=f"mh-uq-{uuid.uuid4().hex[:6]}", description="")
    db_session.add(space)
    await db_session.flush()
    block = IPBlock(space_id=space.id, network="10.71.0.0/16", name="b")
    db_session.add(block)
    await db_session.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network="10.71.1.0/24", name="s")
    db_session.add(subnet)
    await db_session.flush()
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.71.1.5",
        status="allocated",
        hostname="x",
        mac_address="aa:bb:cc:dd:ee:ff",
    )
    db_session.add(ip)
    await db_session.flush()

    for _ in range(3):
        await _record_mac_history(db_session, ip.id, "aa:bb:cc:dd:ee:ff")
    await db_session.commit()

    rows = list(
        (await db_session.execute(select(IpMacHistory).where(IpMacHistory.ip_address_id == ip.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
