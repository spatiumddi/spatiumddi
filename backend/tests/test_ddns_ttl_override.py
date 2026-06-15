"""#428 — DDNS now consumes ddns_ttl + ddns_domain_override.

Both fields were resolved into ``EffectiveDDNS`` but never used in the
publish path. apply_ddns_for_lease now stamps the record TTL with the
effective ddns_ttl and publishes the forward record into the
ddns_domain_override zone (when set + resolvable) instead of the subnet's
default zone.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.dns.ddns import _resolve_override_zone_id, apply_ddns_for_lease


async def _zone(db: AsyncSession, name: str) -> DNSZone:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    zone = DNSZone(
        group_id=grp.id,
        name=name,
        zone_type="primary",
        kind="forward",
        primary_ns="ns1." + name,
        admin_email="admin." + name,
    )
    db.add(zone)
    await db.flush()
    return zone


async def _ddns_subnet(
    db: AsyncSession, *, zone: DNSZone, ttl: int | None, override: str | None = None
) -> tuple[Subnet, IPAddress]:
    space = IPSpace(name=f"d-sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.91.0.0/16", name="d-blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.91.1.0/24",
        name="d-sn",
        dns_zone_id=str(zone.id),
        dns_inherit_settings=False,  # use the subnet's own dns_zone_id
        ddns_enabled=True,
        ddns_inherit_settings=False,
        ddns_hostname_policy="client_or_generated",
        ddns_ttl=ttl,
        ddns_domain_override=override,
    )
    db.add(subnet)
    await db.flush()
    row = IPAddress(
        subnet_id=subnet.id,
        address="10.91.1.50",
        status="dhcp",
        mac_address="aa:bb:cc:dd:ee:01",
        auto_from_lease=True,
    )
    db.add(row)
    await db.flush()
    return subnet, row


async def test_resolve_override_zone_id(db_session: AsyncSession) -> None:
    zone = await _zone(db_session, "ovr.example.")
    await db_session.commit()
    # Matches with or without the trailing dot; None for unknown.
    assert await _resolve_override_zone_id(db_session, "ovr.example") == zone.id
    assert await _resolve_override_zone_id(db_session, "ovr.example.") == zone.id
    assert await _resolve_override_zone_id(db_session, "nope.example") is None
    assert await _resolve_override_zone_id(db_session, None) is None


@pytest.mark.asyncio
async def test_ddns_applies_configured_ttl(db_session: AsyncSession) -> None:
    zone = await _zone(db_session, "ddns.example.")
    subnet, row = await _ddns_subnet(db_session, zone=zone, ttl=321)
    await db_session.commit()

    fired = await apply_ddns_for_lease(
        db_session, subnet=subnet, ipam_row=row, client_hostname="myhost"
    )
    assert fired is True

    rec = (
        await db_session.execute(
            select(DNSRecord).where(DNSRecord.ip_address_id == row.id, DNSRecord.record_type == "A")
        )
    ).scalar_one()
    assert rec.zone_id == zone.id
    assert rec.ttl == 321  # the subnet's ddns_ttl, not the zone default (None)


@pytest.mark.asyncio
async def test_ddns_publishes_into_override_zone(db_session: AsyncSession) -> None:
    default_zone = await _zone(db_session, "default.example.")
    override_zone = await _zone(db_session, "override.example.")
    subnet, row = await _ddns_subnet(
        db_session, zone=default_zone, ttl=None, override="override.example"
    )
    await db_session.commit()

    fired = await apply_ddns_for_lease(
        db_session, subnet=subnet, ipam_row=row, client_hostname="myhost"
    )
    assert fired is True

    rec = (
        await db_session.execute(
            select(DNSRecord).where(DNSRecord.ip_address_id == row.id, DNSRecord.record_type == "A")
        )
    ).scalar_one()
    # The A record landed in the override zone, not the subnet's default.
    assert rec.zone_id == override_zone.id
