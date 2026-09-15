"""spatiumddi#1066 (delete) — the reverse zone a subnet auto-created goes with it.

The default (soft) DELETE trashed the subnet and its scopes and never looked
at the zone: it stayed listed, linked to a subnet the API answered 404 for,
still rendered to the agents — live on nightly-2026.09.13. The permanent path
deleted it with a bare row DELETE, sibling subnets sharing the aggregated /24
included. Now the zone rides the subnet's own deletion batch (one trash entry
to restore, and the restore brings both back), is re-linked to a sibling that
still lives in it, and on the permanent path is deleted the way the zone
operation deletes a zone.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import DNSServerGroup, DNSZone
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services.dns.reverse_zone import reverse_zone_network


async def _admin_headers(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="admin",
        hashed_password=hash_password("password123"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _fixture(db: AsyncSession) -> tuple[str, str, str, str]:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:8]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.67.0.0/16", name="b")
    db.add(block)
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    zone = DNSZone(
        group_id=grp.id,
        name="del.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.del.example.",
        admin_email="admin.del.example.",
    )
    db.add(zone)
    await db.commit()
    return str(space.id), str(block.id), str(grp.id), str(zone.id)


async def _bound_subnet(
    client: AsyncClient, headers: dict[str, str], ids: tuple[str, str, str, str], network: str
) -> str:
    space_id, block_id, gid, zid = ids
    resp = await client.post(
        "/api/v1/ipam/subnets",
        headers=headers,
        json={
            "space_id": space_id,
            "block_id": block_id,
            "network": network,
            "dns_group_id": gid,
            "dns_zone_id": zid,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _zone_row(db: AsyncSession, group_id: str, name: str) -> DNSZone | None:
    """The zone row whether or not it is in the trash, re-read from the database
    (the handler committed through this same session, so the identity map may
    hold a stale copy; populate_existing refreshes it without expiring the
    session, which would make the next attribute read lazy-load)."""
    stmt = (
        select(DNSZone)
        .where(DNSZone.group_id == uuid.UUID(group_id), DNSZone.name == name)
        .execution_options(include_deleted=True, populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _subnet_row(db: AsyncSession, subnet_id: str) -> Subnet | None:
    stmt = (
        select(Subnet)
        .where(Subnet.id == uuid.UUID(subnet_id))
        .execution_options(include_deleted=True, populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def test_reverse_zone_network_inverts_the_products_aggregation() -> None:
    assert reverse_zone_network("250.10.10.in-addr.arpa.") == __import__("ipaddress").ip_network(
        "10.10.250.0/24"
    )
    assert str(reverse_zone_network("10.in-addr.arpa")) == "10.0.0.0/8"
    assert str(reverse_zone_network("8.b.d.0.1.0.0.2.ip6.arpa.")) == "2001:db8::/32"
    assert reverse_zone_network("corp.example.") is None
    assert reverse_zone_network("300.10.in-addr.arpa.") is None


@pytest.mark.asyncio
async def test_soft_delete_trashes_the_zone_with_the_subnet_and_restore_brings_both_back(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    ids = await _fixture(db_session)
    gid = ids[2]
    sid = await _bound_subnet(client, headers, ids, "10.67.1.0/24")
    zone = await _zone_row(db_session, gid, "1.67.10.in-addr.arpa.")
    assert zone is not None and zone.deleted_at is None
    zone_id = zone.id

    resp = await client.delete(f"/api/v1/ipam/subnets/{sid}", headers=headers)
    assert resp.status_code == 204, resp.text

    subnet = await _subnet_row(db_session, sid)
    zone = await _zone_row(db_session, gid, "1.67.10.in-addr.arpa.")
    assert subnet is not None and subnet.deleted_at is not None
    assert zone is not None and zone.deleted_at is not None
    assert zone.deletion_batch_id == subnet.deletion_batch_id  # one trash entry, one restore
    listed = await client.get(f"/api/v1/dns/groups/{gid}/zones", headers=headers)
    assert listed.status_code == 200
    assert str(zone_id) not in {z["id"] for z in listed.json()}

    restored = await client.post(f"/api/v1/admin/trash/subnet/{sid}/restore", headers=headers)
    assert restored.status_code == 200, restored.text
    subnet = await _subnet_row(db_session, sid)
    zone = await _zone_row(db_session, gid, "1.67.10.in-addr.arpa.")
    assert subnet is not None and subnet.deleted_at is None
    assert zone is not None and zone.deleted_at is None
    assert str(zone.linked_subnet_id) == sid
    assert (await client.get(f"/api/v1/ipam/subnets/{sid}", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_soft_delete_relinks_a_shared_zone_to_the_surviving_sibling(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two /25s share one aggregated /24 zone (created for the first, reused
    by the second). Deleting the first must not take the second's PTR zone."""
    headers = await _admin_headers(db_session)
    ids = await _fixture(db_session)
    gid = ids[2]
    first = await _bound_subnet(client, headers, ids, "10.67.2.0/25")
    second = await _bound_subnet(client, headers, ids, "10.67.2.128/25")
    zone = await _zone_row(db_session, gid, "2.67.10.in-addr.arpa.")
    assert zone is not None and str(zone.linked_subnet_id) == first

    resp = await client.delete(f"/api/v1/ipam/subnets/{first}", headers=headers)
    assert resp.status_code == 204, resp.text

    zone = await _zone_row(db_session, gid, "2.67.10.in-addr.arpa.")
    assert zone is not None and zone.deleted_at is None
    assert str(zone.linked_subnet_id) == second
    listed = await client.get(f"/api/v1/dns/groups/{gid}/zones", headers=headers)
    assert str(zone.id) in {z["id"] for z in listed.json()}


@pytest.mark.asyncio
async def test_permanent_delete_removes_the_zone_and_keeps_a_shared_one(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    ids = await _fixture(db_session)
    gid = ids[2]
    alone = await _bound_subnet(client, headers, ids, "10.67.3.0/24")
    first = await _bound_subnet(client, headers, ids, "10.67.4.0/25")
    second = await _bound_subnet(client, headers, ids, "10.67.4.128/25")

    # The permanent path refuses a subnet holding its gateway row without
    # force — the retry the API itself directs.
    resp = await client.delete(
        f"/api/v1/ipam/subnets/{alone}?permanent=true&force=true", headers=headers
    )
    assert resp.status_code == 204, resp.text
    assert await _zone_row(db_session, gid, "3.67.10.in-addr.arpa.") is None
    assert await _subnet_row(db_session, alone) is None

    resp = await client.delete(
        f"/api/v1/ipam/subnets/{first}?permanent=true&force=true", headers=headers
    )
    assert resp.status_code == 204, resp.text
    zone = await _zone_row(db_session, gid, "4.67.10.in-addr.arpa.")
    assert zone is not None and zone.deleted_at is None
    assert str(zone.linked_subnet_id) == second
