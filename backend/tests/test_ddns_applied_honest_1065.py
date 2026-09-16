"""spatiumddi#1065 (second half) — ``ddns_applied`` only when a record op was
enqueued.

With DDNS on, inheritance off and no effective forward zone, ``_sync_dns_record``
returned before enqueuing anything and ``apply_ddns_for_lease`` logged
``ddns_applied`` regardless — live on nightly-2026.09.13: applied with
``zone_override: null``, no record, no fqdn, PTR NXDOMAIN. The sync now says
whether it had anywhere to publish, the apply path logs ``ddns_not_published``
(reason ``no_forward_zone``; a warning for the first lease on a subnet, info
for every later one — never silence, never ``ddns_applied``) instead and
returns False, and the revoke path only claims ``ddns_revoked`` when a record
was retracted.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet
from app.services.dns.ddns import apply_ddns_for_lease, revoke_ddns_for_lease


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
    db: AsyncSession, network: str, host: str, *, zone: DNSZone | None
) -> tuple[Subnet, IPAddress]:
    space = IPSpace(name=f"h-sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.92.0.0/16", name="h-blk")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=network,
        name="h-sn",
        dns_zone_id=str(zone.id) if zone else None,
        dns_inherit_settings=False,  # the subnet's own (empty or bound) DNS settings
        ddns_enabled=True,
        ddns_inherit_settings=False,
        ddns_hostname_policy="client_or_generated",
    )
    db.add(subnet)
    await db.flush()
    row = IPAddress(
        subnet_id=subnet.id,
        address=host,
        status="dhcp",
        mac_address="aa:bb:cc:dd:ee:02",
        auto_from_lease=True,
    )
    db.add(row)
    await db.flush()
    return subnet, row


def _events(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e.get("event") == name]


@pytest.mark.asyncio
async def test_no_forward_zone_is_not_published_and_says_so(db_session: AsyncSession) -> None:
    subnet, row = await _ddns_subnet(db_session, "10.92.1.0/24", "10.92.1.50", zone=None)
    await db_session.commit()

    with capture_logs() as events:
        fired = await apply_ddns_for_lease(
            db_session, subnet=subnet, ipam_row=row, client_hostname="laptop"
        )
    assert fired is False
    assert _events(events, "ddns_applied") == []
    (warned,) = _events(events, "ddns_not_published")
    assert warned["log_level"] == "warning"
    assert warned["reason"] == "no_forward_zone"
    assert warned["ip"] == "10.92.1.50" and warned["hostname"] == "laptop"
    assert "ddns_domain_override" in warned["note"]

    # Nothing was enqueued or written: no record, no zone stamps on the row.
    recs = (
        (await db_session.execute(select(DNSRecord).where(DNSRecord.ip_address_id == row.id)))
        .scalars()
        .all()
    )
    assert recs == []
    assert row.dns_record_id is None and row.fqdn is None


@pytest.mark.asyncio
async def test_every_lease_gets_a_line_the_first_one_a_warning(db_session: AsyncSession) -> None:
    """The first lease DDNS cannot publish on a subnet warns (once per subnet
    per process); every later one — a re-lease of the same address, or the
    agentless lease pull re-evaluating every lease on every poll — still logs
    the event, at info. Never silence: the harness reads the log for the lease
    it just took, and a re-leased address must read the same as a fresh one."""
    subnet, row = await _ddns_subnet(db_session, "10.92.2.0/24", "10.92.2.50", zone=None)
    await db_session.commit()

    with capture_logs() as first:
        await apply_ddns_for_lease(db_session, subnet=subnet, ipam_row=row, client_hostname="x")
    with capture_logs() as second:
        await apply_ddns_for_lease(db_session, subnet=subnet, ipam_row=row, client_hostname="x")
    (w,) = _events(first, "ddns_not_published")
    (i,) = _events(second, "ddns_not_published")
    assert w["log_level"] == "warning" and i["log_level"] == "info"
    assert w["ip"] == i["ip"] == "10.92.2.50"
    assert w["reason"] == i["reason"] == "no_forward_zone"
    assert _events(second, "ddns_applied") == []


@pytest.mark.asyncio
async def test_with_a_forward_zone_it_publishes_and_says_applied(
    db_session: AsyncSession,
) -> None:
    zone = await _zone(db_session, "honest.example.")
    subnet, row = await _ddns_subnet(db_session, "10.92.3.0/24", "10.92.3.50", zone=zone)
    await db_session.commit()

    with capture_logs() as events:
        fired = await apply_ddns_for_lease(
            db_session, subnet=subnet, ipam_row=row, client_hostname="laptop"
        )
    assert fired is True
    (applied,) = _events(events, "ddns_applied")
    assert applied["ip"] == "10.92.3.50"
    assert _events(events, "ddns_not_published") == []
    rec = (
        await db_session.execute(
            select(DNSRecord).where(DNSRecord.ip_address_id == row.id, DNSRecord.record_type == "A")
        )
    ).scalar_one()
    assert rec.zone_id == zone.id
    assert row.dns_record_id == rec.id


@pytest.mark.asyncio
async def test_revoke_claims_nothing_when_nothing_was_published(
    db_session: AsyncSession,
) -> None:
    subnet, row = await _ddns_subnet(db_session, "10.92.4.0/24", "10.92.4.50", zone=None)
    row.hostname = "laptop"  # named by the lease mirror, never published
    await db_session.commit()

    with capture_logs() as events:
        removed = await revoke_ddns_for_lease(db_session, subnet=subnet, ipam_row=row)
    assert removed is False
    assert _events(events, "ddns_revoked") == []


@pytest.mark.asyncio
async def test_revoke_says_revoked_when_a_record_was_retracted(db_session: AsyncSession) -> None:
    zone = await _zone(db_session, "revoke.example.")
    subnet, row = await _ddns_subnet(db_session, "10.92.5.0/24", "10.92.5.50", zone=zone)
    await db_session.commit()
    assert await apply_ddns_for_lease(
        db_session, subnet=subnet, ipam_row=row, client_hostname="laptop"
    )

    with capture_logs() as events:
        removed = await revoke_ddns_for_lease(db_session, subnet=subnet, ipam_row=row)
    assert removed is True
    assert len(_events(events, "ddns_revoked")) == 1
    recs = (
        (await db_session.execute(select(DNSRecord).where(DNSRecord.ip_address_id == row.id)))
        .scalars()
        .all()
    )
    assert recs == []
