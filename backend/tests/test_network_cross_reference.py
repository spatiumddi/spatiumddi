"""Tests for the SNMP → IPAM ARP cross-reference helper.

Three behaviours covered:
  * Existing IP rows in the device's bound space get
    ``last_seen_at`` / ``last_seen_method='snmp'`` refreshed; a
    NULL ``mac_address`` is filled in once but operator-set MACs
    are never overwritten.
  * ``auto_create_discovered=True`` inserts a new
    ``status='discovered'`` row when the IP falls inside a known
    subnet but no row exists yet.
  * ``auto_create_discovered=False`` (the default) keeps the
    counter accurate but skips the insert.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.models.network import NetworkDevice
from app.services.snmp.cross_reference import cross_reference_arp
from app.services.snmp.poller import ArpData


async def _make_space(db: AsyncSession) -> IPSpace:
    space = IPSpace(name=f"snmp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    return space


async def _make_subnet(db: AsyncSession, space: IPSpace, network: str) -> Subnet:
    block = IPBlock(space_id=space.id, network=network, name=f"blk-{uuid.uuid4().hex[:6]}")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=network,
        name=f"sub-{uuid.uuid4().hex[:6]}",
    )
    db.add(subnet)
    await db.flush()
    return subnet


def _make_device(space_id: uuid.UUID, *, auto_create: bool) -> NetworkDevice:
    return NetworkDevice(
        name=f"dev-{uuid.uuid4().hex[:6]}",
        hostname="10.0.0.1",
        ip_address="10.0.0.1",
        snmp_version="v2c",
        ip_space_id=space_id,
        community_encrypted=encrypt_str("public"),
        auto_create_discovered=auto_create,
    )


# ── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_existing_ip_gets_last_seen_and_mac_filled(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space, "10.0.0.0/24")

    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.0.0.42",
        status="allocated",
        mac_address=None,
    )
    db_session.add(ip)
    device = _make_device(space.id, auto_create=False)
    db_session.add(device)
    await db_session.flush()

    counts = await cross_reference_arp(
        db_session,
        device,
        [
            ArpData(
                if_index=1,
                ip_address="10.0.0.42",
                mac_address="aa:bb:cc:dd:ee:01",
                address_type="ipv4",
                state="reachable",
            )
        ],
    )
    await db_session.flush()

    refreshed = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == ip.id))
    ).scalar_one()
    assert refreshed.last_seen_method == "snmp"
    assert refreshed.last_seen_at is not None
    assert str(refreshed.mac_address) == "aa:bb:cc:dd:ee:01"
    assert counts == {"updated": 1, "created": 0, "skipped_no_subnet": 0}


@pytest.mark.asyncio
async def test_existing_ip_with_operator_mac_is_never_overwritten(
    db_session: AsyncSession,
) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space, "10.0.0.0/24")
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.0.0.10",
        status="allocated",
        mac_address="00:11:22:33:44:55",  # operator-set
    )
    db_session.add(ip)
    device = _make_device(space.id, auto_create=False)
    db_session.add(device)
    await db_session.flush()

    await cross_reference_arp(
        db_session,
        device,
        [
            ArpData(
                if_index=1,
                ip_address="10.0.0.10",
                mac_address="aa:bb:cc:dd:ee:99",
                address_type="ipv4",
                state="reachable",
            )
        ],
    )
    await db_session.flush()
    refreshed = (
        await db_session.execute(select(IPAddress).where(IPAddress.id == ip.id))
    ).scalar_one()
    # Operator MAC preserved.
    assert str(refreshed.mac_address) == "00:11:22:33:44:55"


@pytest.mark.asyncio
async def test_auto_create_inserts_discovered_row(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    subnet = await _make_subnet(db_session, space, "10.0.0.0/24")
    device = _make_device(space.id, auto_create=True)
    db_session.add(device)
    await db_session.flush()

    counts = await cross_reference_arp(
        db_session,
        device,
        [
            ArpData(
                if_index=1,
                ip_address="10.0.0.77",
                mac_address="aa:bb:cc:dd:ee:77",
                address_type="ipv4",
                state="reachable",
            )
        ],
    )
    await db_session.flush()

    new_ip = (
        await db_session.execute(
            select(IPAddress).where(
                IPAddress.subnet_id == subnet.id, IPAddress.address == "10.0.0.77"
            )
        )
    ).scalar_one_or_none()
    assert new_ip is not None
    assert new_ip.status == "discovered"
    assert str(new_ip.mac_address) == "aa:bb:cc:dd:ee:77"
    assert new_ip.last_seen_method == "snmp"
    assert counts == {"updated": 0, "created": 1, "skipped_no_subnet": 0}


@pytest.mark.asyncio
async def test_auto_create_off_skips_insert(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    await _make_subnet(db_session, space, "10.0.0.0/24")
    device = _make_device(space.id, auto_create=False)
    db_session.add(device)
    await db_session.flush()

    counts = await cross_reference_arp(
        db_session,
        device,
        [
            ArpData(
                if_index=1,
                ip_address="10.0.0.5",
                mac_address="aa:bb:cc:dd:ee:05",
                address_type="ipv4",
                state="reachable",
            )
        ],
    )
    await db_session.flush()
    rows = list(
        (await db_session.execute(select(IPAddress).where(IPAddress.address == "10.0.0.5")))
        .scalars()
        .all()
    )
    assert rows == []
    assert counts == {"updated": 0, "created": 0, "skipped_no_subnet": 1}


@pytest.mark.asyncio
async def test_no_matching_subnet_increments_skipped(db_session: AsyncSession) -> None:
    space = await _make_space(db_session)
    # No subnet in the space.
    device = _make_device(space.id, auto_create=True)
    db_session.add(device)
    await db_session.flush()

    counts = await cross_reference_arp(
        db_session,
        device,
        [
            ArpData(
                if_index=1,
                ip_address="192.168.99.1",
                mac_address="aa:bb:cc:dd:ee:ff",
                address_type="ipv4",
                state="reachable",
            )
        ],
    )
    assert counts == {"updated": 0, "created": 0, "skipped_no_subnet": 1}
