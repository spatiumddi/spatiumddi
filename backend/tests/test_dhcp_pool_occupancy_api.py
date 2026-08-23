"""DHCP pool occupancy over REST (issue #913).

``services/dhcp/pool_occupancy.py`` has computed this since #339 and no
HTTP route called it, so "is this pool exhausted?" — the first question
asked when a client cannot get an address — could only be answered by
re-deriving it from three other endpoints. These tests cover the two
things a re-derivation gets wrong: reservations withhold an address even
when the device is offline, and a prefix-delegation pool has no range to
divide by.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import (
    DHCPLease,
    DHCPPool,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
    DHCPStaticAssignment,
)
from app.models.ipam import IPBlock, IPSpace, Subnet

CIDR = "10.61.0.0/24"


async def _token(db: AsyncSession) -> str:
    user = User(
        username=f"occ-{uuid.uuid4().hex[:6]}",
        email=f"occ-{uuid.uuid4().hex[:6]}@example.test",
        display_name="occ",
        hashed_password=hash_password("x"),
        auth_source="local",
        is_superadmin=True,
    )
    user.groups = []
    db.add(user)
    await db.flush()
    return create_access_token(str(user.id))


async def _scope(db: AsyncSession) -> tuple[DHCPScope, DHCPServer]:
    space = IPSpace(name=f"s-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=CIDR, name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=CIDR, name="s")
    group = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, group])
    await db.flush()
    scope = DHCPScope(group_id=group.id, subnet_id=subnet.id, name="scope-a")
    server = DHCPServer(name=f"kea-{uuid.uuid4().hex[:6]}", host="10.0.0.1", driver="kea")
    db.add_all([scope, server])
    await db.flush()
    return scope, server


@pytest.mark.asyncio
async def test_pool_occupancy_counts_leases_and_offline_reservations(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Counting leases alone under-reports exhaustion (#631).

    A reserved address is withheld from the dynamic set whether or not
    the device is currently online, and a reserved-AND-leased address is
    one address, not two.
    """
    scope, server = await _scope(db_session)
    pool = DHCPPool(
        scope_id=scope.id,
        name="pool-a",
        start_ip="10.61.0.10",
        end_ip="10.61.0.19",  # 10 addresses inclusive
        pool_type="dynamic",
    )
    db_session.add(pool)
    db_session.add_all(
        [
            DHCPLease(
                server_id=server.id,
                scope_id=scope.id,
                ip_address="10.61.0.10",
                mac_address="aa:bb:cc:00:00:01",
                state="active",
            ),
            # Reserved AND currently leased — one address, counted once.
            DHCPLease(
                server_id=server.id,
                scope_id=scope.id,
                ip_address="10.61.0.11",
                mac_address="aa:bb:cc:00:00:02",
                state="active",
            ),
            DHCPStaticAssignment(
                scope_id=scope.id,
                ip_address="10.61.0.11",
                mac_address="aa:bb:cc:00:00:02",
            ),
            # Reserved and offline — still unavailable to a dynamic client.
            DHCPStaticAssignment(
                scope_id=scope.id,
                ip_address="10.61.0.12",
                mac_address="aa:bb:cc:00:00:03",
            ),
            # Outside the range — must not count.
            DHCPLease(
                server_id=server.id,
                scope_id=scope.id,
                ip_address="10.61.0.50",
                mac_address="aa:bb:cc:00:00:04",
                state="active",
            ),
        ]
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get(f"/api/v1/dhcp/pools/{pool.id}/occupancy", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 10
    assert body["assigned"] == 3
    assert body["free"] == 7
    assert body["percent"] == 30.0
    assert body["computed_at"]


@pytest.mark.asyncio
async def test_scope_occupancy_returns_every_range_pool(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The shape that matters: a scope-level "fine" can hide one full
    class-restricted pool."""
    scope, _ = await _scope(db_session)
    db_session.add_all(
        [
            DHCPPool(
                scope_id=scope.id,
                name="voice",
                start_ip="10.61.0.10",
                end_ip="10.61.0.19",
                pool_type="dynamic",
            ),
            DHCPPool(
                scope_id=scope.id,
                name="data",
                start_ip="10.61.0.20",
                end_ip="10.61.0.29",
                pool_type="dynamic",
            ),
        ]
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    res = await client.get(f"/api/v1/dhcp/scopes/{scope.id}/pools/occupancy", headers=headers)
    assert res.status_code == 200, res.text
    rows = res.json()
    assert [r["pool_name"] for r in rows] == ["voice", "data"]
    assert all(r["assigned"] == 0 and r["total"] == 10 for r in rows)


@pytest.mark.asyncio
async def test_only_dynamic_pools_get_an_occupancy(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The other three types would each produce a misleading number.

    A ``pd`` pool stores its prefix's network address in both range ends
    as NOT NULL placeholders, so dividing by that yields a one-address
    pool at 0%. An ``excluded`` range is never offered to a client, so a
    percentage full is not a fact about it. A ``reserved`` range is held
    for static assignments and is *supposed* to fill up, so reporting it
    on the same scale renders a correctly-configured one as a red
    exhaustion bar. Answering only for dynamic pools also keeps this
    endpoint agreeing with the ``dhcp_pool_exhaustion`` alert evaluator
    and the ``find_dhcp_pool_occupancy`` copilot tool, which both filter
    the same way — a disagreement between those is exactly how a wrong
    "the pool is fine" gets produced.
    """
    scope, _ = await _scope(db_session)
    pd = DHCPPool(
        scope_id=scope.id,
        name="delegation",
        start_ip="2001:db8::",
        end_ip="2001:db8::",
        pool_type="pd",
        pd_prefix="2001:db8::/48",
        delegated_length=56,
    )
    excluded = DHCPPool(
        scope_id=scope.id,
        name="infra",
        start_ip="10.61.0.1",
        end_ip="10.61.0.9",
        pool_type="excluded",
    )
    reserved = DHCPPool(
        scope_id=scope.id,
        name="printers",
        start_ip="10.61.0.30",
        end_ip="10.61.0.39",
        pool_type="reserved",
    )
    dynamic = DHCPPool(
        scope_id=scope.id,
        name="workstations",
        start_ip="10.61.0.100",
        end_ip="10.61.0.109",
        pool_type="dynamic",
    )
    db_session.add_all([pd, excluded, reserved, dynamic])
    await db_session.flush()

    headers = {"Authorization": f"Bearer {await _token(db_session)}"}

    res = await client.get(f"/api/v1/dhcp/pools/{pd.id}/occupancy", headers=headers)
    assert res.status_code == 422
    assert "prefix-delegation" in res.text.lower()

    for pool in (excluded, reserved):
        res = await client.get(f"/api/v1/dhcp/pools/{pool.id}/occupancy", headers=headers)
        assert res.status_code == 422, pool.pool_type
        assert pool.pool_type in res.text

    listed = await client.get(f"/api/v1/dhcp/scopes/{scope.id}/pools/occupancy", headers=headers)
    assert [r["pool_name"] for r in listed.json()] == ["workstations"]


@pytest.mark.asyncio
async def test_occupancy_404s_for_unknown_ids(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = {"Authorization": f"Bearer {await _token(db_session)}"}
    await db_session.flush()
    missing = uuid.uuid4()
    assert (
        await client.get(f"/api/v1/dhcp/pools/{missing}/occupancy", headers=headers)
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/dhcp/scopes/{missing}/pools/occupancy", headers=headers)
    ).status_code == 404
