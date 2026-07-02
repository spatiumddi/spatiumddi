"""#481 — DNS drift is IPv6-aware (AAAA), not A-only.

compute_subnet_dns_drift filtered ``record_type == "A"`` only, so for an IPv6
subnet a named IP with no AAAA record was reported as a nonsensical "missing A"
that never cleared (the apply path writes AAAA, which the classifier couldn't
see), and a correctly-synced AAAA looked like drift forever. The classifier is
now family-aware.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.dns.sync_check import compute_subnet_dns_drift


async def _v6_subnet_with_zone(db: AsyncSession) -> tuple[Subnet, DNSZone]:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    zone = DNSZone(
        group_id=grp.id,
        name="v6.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.v6.example.",
        admin_email="admin.v6.example.",
    )
    db.add(zone)
    await db.flush()
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="2001:db8::/32", name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="2001:db8:0:1::/64",
        name="sn",
        dns_zone_id=str(zone.id),
        dns_inherit_settings=False,
    )
    db.add(subnet)
    await db.flush()
    return subnet, zone


@pytest.mark.asyncio
async def test_missing_aaaa_reported_for_v6_ip(db_session: AsyncSession) -> None:
    subnet, _zone = await _v6_subnet_with_zone(db_session)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="2001:db8:0:1::10",
        status="allocated",
        hostname="host6",
    )
    db_session.add(ip)
    await db_session.flush()
    await db_session.refresh(ip)
    await db_session.commit()

    report = await compute_subnet_dns_drift(db_session, subnet.id)
    # The named v6 IP with no AAAA is drift — reported as missing AAAA, not A.
    assert len(report.missing) == 1
    assert report.missing[0].record_type == "AAAA"
    assert report.missing[0].expected_value == str(ip.address)


@pytest.mark.asyncio
async def test_present_aaaa_is_not_drift(db_session: AsyncSession) -> None:
    subnet, zone = await _v6_subnet_with_zone(db_session)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="2001:db8:0:1::10",
        status="allocated",
        hostname="host6",
    )
    db_session.add(ip)
    await db_session.flush()
    await db_session.refresh(ip)
    rec = DNSRecord(
        zone_id=zone.id,
        name="host6",
        fqdn="host6.v6.example.",
        record_type="AAAA",
        value=str(ip.address),
        auto_generated=True,
        ip_address_id=ip.id,
    )
    db_session.add(rec)
    await db_session.flush()
    await db_session.commit()

    report = await compute_subnet_dns_drift(db_session, subnet.id)
    # A correctly-synced AAAA is NOT drift (was a perpetual false-positive
    # "missing A" before the fix).
    assert report.missing == []
    assert report.mismatched == []
