"""DHCP scope deletion tears down dynamic leases + DHCP-derived IPAM mirrors (#623).

#621 fixed the scope-delete cascade for pools + reservations, but left two things
dangling that the operator sees on the frontend:

  * the reservation's IPAM mirror (``status="static_dhcp"``) was never released on
    the soft path — and even the permanent path only *freed* it to ``available``,
    which still renders as a visible row;
  * dynamic leases (``DHCPLease`` + their ``status="dhcp"`` / ``auto_from_lease``
    mirror) were never touched at all — they have only a nullable
    ``ON DELETE SET NULL`` backlink to the scope.

Both kept showing in the subnet IP table and counting toward utilization after the
scope was gone. Scope deletion now DELETES these DHCP-derived rows (so the IPs fold
back into free gaps), and a Trash restore re-creates the reservation mirrors.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import (
    DHCPLease,
    DHCPLeaseHistory,
    DHCPScope,
    DHCPServer,
    DHCPServerGroup,
    DHCPStaticAssignment,
)
from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet

_STATIC_IP = "10.50.0.10"
_STATIC_MAC = "aa:bb:cc:dd:ee:10"
_LEASE_IP = "10.50.0.150"
_LEASE_MAC = "aa:bb:cc:dd:ee:99"


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"lt-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Lease Teardown Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_scope(
    db: AsyncSession, *, network: str = "10.50.0.0/24"
) -> tuple[DHCPScope, Subnet, DHCPServerGroup]:
    space = IPSpace(name=f"lt-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="root")
    db.add(block)
    await db.flush()
    subnet = Subnet(space_id=space.id, block_id=block.id, network=network, name="lan")
    db.add(subnet)
    await db.flush()
    group = DHCPServerGroup(name=f"lt-{uuid.uuid4().hex[:6]}")
    db.add(group)
    await db.flush()
    db.add(
        DHCPServer(
            name=f"lt-{uuid.uuid4().hex[:6]}",
            driver="kea",
            host="127.0.0.1",
            server_group_id=group.id,
        )
    )
    scope = DHCPScope(
        group_id=group.id,
        subnet_id=subnet.id,
        name="teardown-scope",
        address_family="ipv4",
    )
    db.add(scope)
    await db.flush()
    return scope, subnet, group


async def _seed_scope_with_static_and_lease(
    client: AsyncClient, db: AsyncSession, headers: dict[str, str]
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A scope with one reservation (+ its static_dhcp mirror, created via the API)
    and one dynamic lease (+ its auto_from_lease ``dhcp`` mirror). Returns
    ``(scope_id, subnet_id, server_id)``."""
    scope, subnet, group = await _make_scope(db)
    server = (
        await db.execute(select(DHCPServer).where(DHCPServer.server_group_id == group.id))
    ).scalar_one()
    scope_id, subnet_id, server_id = scope.id, subnet.id, server.id
    await db.commit()

    created = await client.post(
        f"/api/v1/dhcp/scopes/{scope_id}/statics",
        headers=headers,
        json={"ip_address": _STATIC_IP, "mac_address": _STATIC_MAC, "hostname": "res"},
    )
    assert created.status_code == 201, created.text

    # A dynamic lease + its auto_from_lease IPAM mirror, as pull_leases would create.
    db.add(
        IPAddress(
            subnet_id=subnet_id,
            address=_LEASE_IP,
            status="dhcp",
            mac_address=_LEASE_MAC,
            auto_from_lease=True,
        )
    )
    await db.flush()
    db.add(
        DHCPLease(
            server_id=server_id,
            scope_id=scope_id,
            ip_address=_LEASE_IP,
            mac_address=_LEASE_MAC,
            state="active",
        )
    )
    await db.commit()
    return scope_id, subnet_id, server_id


async def _subnet_addresses(db: AsyncSession, subnet_id: uuid.UUID) -> set[str]:
    rows = (
        (await db.execute(select(IPAddress).where(IPAddress.subnet_id == subnet_id)))
        .scalars()
        .all()
    )
    return {str(r.address) for r in rows}


@pytest.mark.asyncio
async def test_soft_scope_delete_purges_lease_and_mirrors(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope_id, subnet_id, server_id = await _seed_scope_with_static_and_lease(
        client, db_session, headers
    )

    resp = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    db_session.expire_all()

    # Both DHCP-derived mirror rows are DELETED → the IPs fold into free gaps.
    addrs = await _subnet_addresses(db_session, subnet_id)
    assert _STATIC_IP not in addrs
    assert _LEASE_IP not in addrs

    # The dynamic lease row is gone, with a ``removed`` history stamp.
    leases = (
        (await db_session.execute(select(DHCPLease).where(DHCPLease.server_id == server_id)))
        .scalars()
        .all()
    )
    assert leases == []
    hist = (
        (
            await db_session.execute(
                select(DHCPLeaseHistory).where(DHCPLeaseHistory.server_id == server_id)
            )
        )
        .scalars()
        .all()
    )
    assert any(h.lease_state == "removed" for h in hist)

    # Scope + reservation are soft-deleted (restorable), not hard-gone.
    sc = (
        await db_session.execute(
            select(DHCPScope)
            .where(DHCPScope.id == scope_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one()
    assert sc.deleted_at is not None
    statics = (
        (
            await db_session.execute(
                select(DHCPStaticAssignment)
                .where(DHCPStaticAssignment.scope_id == scope_id)
                .execution_options(include_deleted=True)
            )
        )
        .scalars()
        .all()
    )
    assert statics and all(s.deleted_at is not None for s in statics)


@pytest.mark.asyncio
async def test_permanent_scope_delete_purges_lease_and_mirrors(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope_id, subnet_id, server_id = await _seed_scope_with_static_and_lease(
        client, db_session, headers
    )

    resp = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}?permanent=true", headers=headers)
    assert resp.status_code == 204, resp.text
    db_session.expire_all()

    addrs = await _subnet_addresses(db_session, subnet_id)
    assert _STATIC_IP not in addrs
    assert _LEASE_IP not in addrs
    # Lease is gone (not a scope_id-SET-NULL survivor), scope is hard-gone.
    assert (
        await db_session.execute(select(DHCPLease).where(DHCPLease.server_id == server_id))
    ).scalars().all() == []
    assert (
        await db_session.execute(
            select(DHCPScope)
            .where(DHCPScope.id == scope_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_subnet_cascade_delete_purges_scope_leases(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope_id, subnet_id, server_id = await _seed_scope_with_static_and_lease(
        client, db_session, headers
    )

    resp = await client.delete(f"/api/v1/ipam/subnets/{subnet_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    db_session.expire_all()

    # The subnet-cascade batch reaches the scope, so its leases + static mirror
    # are torn down (the lease ROW is the piece the subnet-mirror sweep never
    # handled).
    assert (
        await db_session.execute(select(DHCPLease).where(DHCPLease.server_id == server_id))
    ).scalars().all() == []
    addrs = await _subnet_addresses(db_session, subnet_id)
    assert _STATIC_IP not in addrs
    assert _LEASE_IP not in addrs


@pytest.mark.asyncio
async def test_restore_recreates_static_mirror(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope_id, subnet_id, _server_id = await _seed_scope_with_static_and_lease(
        client, db_session, headers
    )

    resp = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    db_session.expire_all()
    assert _STATIC_IP not in await _subnet_addresses(db_session, subnet_id)

    restored = await client.post(
        f"/api/v1/admin/trash/dhcp_scope/{scope_id}/restore", headers=headers
    )
    assert restored.status_code == 200, restored.text
    db_session.expire_all()

    # The reservation mirror is re-created (status + back-link), so the IP shows
    # as a reservation again. Leases are NOT restored (they re-populate from the
    # next poll) — that's why hard-delete is the right symmetry.
    mirror = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet_id, IPAddress.address == _STATIC_IP
            )
        )
    ).scalar_one_or_none()
    assert mirror is not None
    assert mirror.status == "static_dhcp"
    assert mirror.static_assignment_id is not None


@pytest.mark.asyncio
async def test_restore_preserves_operator_metadata(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A delete→restore cycle must not lose operator-authored columns on the
    reservation's IPAM mirror (#630). The mirror is hard-deleted (so a freed row
    doesn't linger + inflate utilization), so the operator fields are snapshotted
    onto the retained reservation and re-applied on restore."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope_id, subnet_id, _server_id = await _seed_scope_with_static_and_lease(
        client, db_session, headers
    )

    # Operator edits the reservation's mirror: description + tags + custom fields.
    mirror = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet_id, IPAddress.address == _STATIC_IP
            )
        )
    ).scalar_one()
    mirror.description = "printer in room 3"
    mirror.tags = {"env": "lab"}
    mirror.custom_fields = {"asset": "A-42"}
    mirror.role = "reserved-role"
    await db_session.commit()

    resp = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    db_session.expire_all()
    assert _STATIC_IP not in await _subnet_addresses(db_session, subnet_id)

    restored = await client.post(
        f"/api/v1/admin/trash/dhcp_scope/{scope_id}/restore", headers=headers
    )
    assert restored.status_code == 200, restored.text
    db_session.expire_all()

    recreated = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet_id, IPAddress.address == _STATIC_IP
            )
        )
    ).scalar_one()
    assert recreated.description == "printer in room 3"
    assert recreated.tags == {"env": "lab"}
    assert recreated.custom_fields == {"asset": "A-42"}
    assert recreated.role == "reserved-role"


@pytest.mark.asyncio
async def test_delete_lease_endpoint_purges_row_and_mirror(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The standalone DELETE endpoint shares the same purge_lease helper (DRY)."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    scope_id, subnet_id, server_id = await _seed_scope_with_static_and_lease(
        client, db_session, headers
    )
    lease = (
        await db_session.execute(select(DHCPLease).where(DHCPLease.server_id == server_id))
    ).scalar_one()
    lease_id = lease.id

    resp = await client.delete(
        f"/api/v1/dhcp/servers/{server_id}/leases/{lease_id}", headers=headers
    )
    assert resp.status_code == 204, resp.text
    db_session.expire_all()

    assert (
        await db_session.execute(select(DHCPLease).where(DHCPLease.id == lease_id))
    ).scalar_one_or_none() is None
    assert _LEASE_IP not in await _subnet_addresses(db_session, subnet_id)


@pytest.mark.asyncio
async def test_soft_scope_delete_removes_static_dns_record(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Deleting a scope must remove its reservations' auto-generated A records —
    even in a zone with NO primary DNS server, where the wire delete can't be
    pushed. Otherwise the record is left as an orphaned "ip-deleted" stale row
    that Sync DNS can't clear (#623)."""
    _, token = await _make_admin(db_session)
    headers = {"Authorization": f"Bearer {token}"}

    # DNS group with NO server (no primary) + a forward zone.
    dns_grp = DNSServerGroup(name=f"lt-{uuid.uuid4().hex[:6]}")
    db_session.add(dns_grp)
    await db_session.flush()
    zone = DNSZone(
        group_id=dns_grp.id,
        name="lt.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.lt.example.",
        admin_email="admin.lt.example.",
    )
    db_session.add(zone)
    await db_session.flush()

    # Scope + subnet linked to the zone so the static create publishes an A record.
    scope, subnet, _g = await _make_scope(db_session)
    subnet.dns_zone_id = str(zone.id)
    subnet.dns_inherit_settings = False
    scope_id, zone_id = scope.id, zone.id
    await db_session.commit()

    created = await client.post(
        f"/api/v1/dhcp/scopes/{scope_id}/statics",
        headers=headers,
        json={"ip_address": _STATIC_IP, "mac_address": _STATIC_MAC, "hostname": "res"},
    )
    assert created.status_code == 201, created.text

    async def _zone_a_records() -> list[DNSRecord]:
        return list(
            (
                await db_session.execute(
                    select(DNSRecord).where(
                        DNSRecord.zone_id == zone_id,
                        DNSRecord.auto_generated.is_(True),
                        DNSRecord.record_type == "A",
                    )
                )
            )
            .scalars()
            .all()
        )

    db_session.expire_all()
    # The A record landed in the DB even with no primary (wire push just dropped).
    assert any(r.name == "res" for r in await _zone_a_records())

    resp = await client.delete(f"/api/v1/dhcp/scopes/{scope_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    db_session.expire_all()

    # Deleted with the reservation — no orphaned "ip-deleted" stale record left.
    assert await _zone_a_records() == []
