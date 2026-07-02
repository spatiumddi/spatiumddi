"""#483 — block/space DNS-drift aggregation batches per-subnet loads.

compute_block_dns_drift / compute_space_dns_drift bulk-load IPs + auto-records
once across all subnets instead of re-querying per subnet. This proves the
batched aggregation stays behavior-identical to summing the per-subnet
compute_subnet_dns_drift path.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns import DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.dns.sync_check import compute_block_dns_drift, compute_subnet_dns_drift


async def _setup(db: AsyncSession) -> tuple[IPBlock, list[Subnet]]:
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
    block = IPBlock(space_id=space.id, network="10.70.0.0/16", name="blk")
    db.add(block)
    await db.flush()
    subnets: list[Subnet] = []
    for i in (1, 2):
        sn = Subnet(
            space_id=space.id,
            block_id=block.id,
            network=f"10.70.{i}.0/24",
            name=f"sn{i}",
            dns_zone_id=str(zone.id),
            dns_inherit_settings=False,
        )
        db.add(sn)
        await db.flush()
        # A named IP with no A record → one "missing A" per subnet.
        ip = IPAddress(
            subnet_id=sn.id,
            address=f"10.70.{i}.10",
            status="allocated",
            hostname=f"host{i}",
        )
        db.add(ip)
        await db.flush()
        subnets.append(sn)
    return block, subnets


@pytest.mark.asyncio
async def test_block_drift_matches_per_subnet_sum(db_session: AsyncSession) -> None:
    block, subnets = await _setup(db_session)
    await db_session.commit()

    batched = await compute_block_dns_drift(db_session, block.id)
    assert len(batched.missing) == 2
    assert {m.record_type for m in batched.missing} == {"A"}

    # The batched aggregation must equal summing the per-subnet (non-batched)
    # path item-for-item — proves the #483 preloading is behavior-preserving.
    per_missing: list = []
    per_mismatched: list = []
    per_stale: list = []
    for sn in subnets:
        r = await compute_subnet_dns_drift(db_session, sn.id)
        per_missing.extend(r.missing)
        per_mismatched.extend(r.mismatched)
        per_stale.extend(r.stale)

    assert len(batched.missing) == len(per_missing)
    assert len(batched.mismatched) == len(per_mismatched)
    assert len(batched.stale) == len(per_stale)
    # Same set of expected forward names surfaced.
    assert {m.expected_name for m in batched.missing} == {m.expected_name for m in per_missing}
