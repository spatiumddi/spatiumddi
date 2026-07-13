"""Per-scope opt-out of DNS drift for dynamic-pool lease mirrors.

A pulled DHCP lease is mirrored into IPAM as an ``auto_from_lease`` row carrying
the client-supplied hostname, so the IPAM↔DNS drift check would flag every
ephemeral lease with no DNS record as "missing" (out of sync). When a scope sets
``dns_track_dynamic_leases=False``, its dynamic-pool lease mirrors are excluded
from the drift report; a manually-allocated IP in the same range is still
tracked, and static reservations are never affected.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPPool, DHCPScope, DHCPServerGroup
from app.models.dns import DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.dns.sync_check import compute_subnet_dns_drift

CIDR = "10.71.0.0/24"


async def _setup(db: AsyncSession, *, track: bool) -> Subnet:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    zone = DNSZone(
        group_id=grp.id,
        name="corp.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.corp.example.",
        admin_email="admin.corp.example.",
    )
    db.add(zone)
    await db.flush()
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network=CIDR, name="blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=CIDR,
        name="sn",
        dns_zone_id=str(zone.id),
        dns_inherit_settings=False,
    )
    dhcp_grp = DHCPServerGroup(name=f"dg-{uuid.uuid4().hex[:6]}")
    db.add_all([subnet, dhcp_grp])
    await db.flush()
    scope = DHCPScope(
        group_id=dhcp_grp.id,
        subnet_id=subnet.id,
        name="sc",
        is_active=True,
        dns_track_dynamic_leases=track,
    )
    db.add(scope)
    await db.flush()
    db.add(
        DHCPPool(
            scope_id=scope.id,
            pool_type="dynamic",
            start_ip="10.71.0.100",
            end_ip="10.71.0.200",
            name="p",
        )
    )
    # A pulled dynamic lease inside the pool (auto_from_lease + hostname).
    db.add(
        IPAddress(
            subnet_id=subnet.id,
            address="10.71.0.150",
            status="dhcp",
            hostname="lease-host",
            auto_from_lease=True,
        )
    )
    # Control: a manually-allocated IP with a hostname OUTSIDE the pool — always
    # drift-checked regardless of the flag.
    db.add(
        IPAddress(
            subnet_id=subnet.id,
            address="10.71.0.10",
            status="allocated",
            hostname="static-host",
        )
    )
    await db.flush()
    return subnet


async def test_dynamic_lease_tracked_by_default(db_session: AsyncSession) -> None:
    subnet = await _setup(db_session, track=True)
    report = await compute_subnet_dns_drift(db_session, subnet.id)
    missing_ips = {m.ip_address for m in report.missing}
    # Both the lease mirror and the manual IP are flagged missing.
    assert "10.71.0.150" in missing_ips
    assert "10.71.0.10" in missing_ips


async def test_dynamic_lease_excluded_when_opted_out(db_session: AsyncSession) -> None:
    subnet = await _setup(db_session, track=False)
    report = await compute_subnet_dns_drift(db_session, subnet.id)
    missing_ips = {m.ip_address for m in report.missing}
    # The dynamic-pool lease mirror is excluded...
    assert "10.71.0.150" not in missing_ips
    # ...but the manually-allocated IP outside the pool is still tracked.
    assert "10.71.0.10" in missing_ips
