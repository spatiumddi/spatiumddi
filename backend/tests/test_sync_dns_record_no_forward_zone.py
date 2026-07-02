"""#480 — _sync_dns_record must not crash when an IP has no effective forward
zone (fqdn is None) but a reverse zone still resolves.

A PTR points AT the forward FQDN. With no primary forward zone ``fqdn`` is
None, and the reverse-PTR block used to do ``ptr_value = fqdn + "."`` →
``TypeError: NoneType + str``. The early-return guard only bailed when BOTH
``effective_zone_id is None`` AND ``extra_zone_ids`` was empty, so a split-
horizon IP with ``extra_zone_ids`` set but no forward zone (reachable through
the public create endpoint) sailed past it into the crash.

This pins the guard: the call returns cleanly and publishes no PTR.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.ipam.router import _sync_dns_record
from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet


async def _subnet_with_reverse_only(db: AsyncSession) -> tuple[Subnet, DNSZone]:
    """A subnet with a linked reverse zone but NO forward zone."""
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.80.0.0/16", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network="10.80.1.0/24",
        name="s",
        dns_zone_id=None,  # no forward zone
        dns_inherit_settings=False,
    )
    db.add(subnet)
    await db.flush()
    rev = DNSZone(
        group_id=grp.id,
        name="1.80.10.in-addr.arpa",
        zone_type="primary",
        kind="reverse",
        linked_subnet_id=subnet.id,
        primary_ns="ns1.example.",
        admin_email="admin.example.",
    )
    db.add(rev)
    await db.flush()
    return subnet, rev


@pytest.mark.asyncio
async def test_extra_zone_ids_without_forward_zone_does_not_crash(
    db_session: AsyncSession,
) -> None:
    subnet, _rev = await _subnet_with_reverse_only(db_session)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.80.1.50",
        status="allocated",
        hostname="host",
        # Non-empty extra_zone_ids bypasses the early-return guard; a bogus id
        # is skipped by the forward fan-out (db.get -> None -> continue), so we
        # land in the reverse-PTR block with fqdn=None — the crash repro.
        extra_zone_ids=[str(uuid.uuid4())],
    )
    db_session.add(ip)
    await db_session.flush()

    # Must not raise (was TypeError: NoneType + str).
    await _sync_dns_record(db_session, ip, subnet)
    await db_session.flush()

    # No PTR published — there is no forward name to point at.
    ptrs = (
        (
            await db_session.execute(
                select(DNSRecord).where(
                    DNSRecord.ip_address_id == ip.id,
                    DNSRecord.record_type == "PTR",
                )
            )
        )
        .scalars()
        .all()
    )
    assert ptrs == []


@pytest.mark.asyncio
async def test_stale_ptr_is_retracted_when_forward_zone_gone(
    db_session: AsyncSession,
) -> None:
    # When the primary forward zone was deleted, an IP that HAD a PTR reaches
    # the fqdn=None path — the guard must RETRACT the now-orphaned PTR rather
    # than leave it pointing at a name that can no longer be generated (Copilot
    # review on #480).
    subnet, rev = await _subnet_with_reverse_only(db_session)
    ip = IPAddress(
        subnet_id=subnet.id,
        address="10.80.1.60",
        status="allocated",
        hostname="host",
        extra_zone_ids=[str(uuid.uuid4())],  # fqdn=None path
    )
    db_session.add(ip)
    await db_session.flush()
    # A pre-existing auto-generated PTR, as if a forward zone once existed.
    db_session.add(
        DNSRecord(
            zone_id=rev.id,
            name="60",
            fqdn="60.1.80.10.in-addr.arpa.",
            record_type="PTR",
            value="host.old-forward.example.",
            auto_generated=True,
            ip_address_id=ip.id,
        )
    )
    await db_session.flush()

    await _sync_dns_record(db_session, ip, subnet)
    await db_session.flush()

    remaining = (
        (
            await db_session.execute(
                select(DNSRecord).where(
                    DNSRecord.ip_address_id == ip.id,
                    DNSRecord.record_type == "PTR",
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []  # the stale PTR was retracted, not left orphaned
