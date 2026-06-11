"""Active IP-reconciliation hygiene alert matchers (#369).

Covers the three new matchers against seeded IPAddress + ip_mac_history rows:
free-but-responding, stale-reservation, and unknown-MAC-in-static-range.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertRule
from app.models.ipam import IpMacHistory, IPAddress, IPBlock, IPSpace, Subnet
from app.services.alerts import (
    RULE_TYPE_IP_FREE_BUT_RESPONDING,
    RULE_TYPE_STALE_RESERVATION,
    RULE_TYPE_UNKNOWN_MAC_IN_STATIC_RANGE,
    _matching_ip_free_but_responding_subjects,
    _matching_stale_reservation_subjects,
    _matching_unknown_mac_in_static_range_subjects,
)


async def _make_subnet(db: AsyncSession) -> Subnet:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=f"10.{uuid.uuid4().int % 250}.0.0/24",
        name="sn",
        total_ips=254,
    )
    db.add(subnet)
    await db.flush()
    return subnet


async def _ip(
    db: AsyncSession,
    subnet: Subnet,
    address: str,
    *,
    status: str,
    seen_days_ago: float | None,
    mac: str | None = None,
) -> IPAddress:
    last_seen = None
    if seen_days_ago is not None:
        last_seen = datetime.now(UTC) - timedelta(days=seen_days_ago)
    row = IPAddress(
        subnet_id=subnet.id,
        address=address,
        status=status,
        last_seen_at=last_seen,
        last_seen_method="ping" if last_seen else None,
        mac_address=mac,
    )
    db.add(row)
    await db.flush()
    return row


def _rule(rule_type: str, days: int) -> AlertRule:
    return AlertRule(name="t", rule_type=rule_type, severity="info", threshold_days=days)


# ── free but responding ─────────────────────────────────────────────────


async def test_free_but_responding(db_session: AsyncSession) -> None:
    subnet = await _make_subnet(db_session)
    hit = await _ip(db_session, subnet, "10.9.0.10", status="available", seen_days_ago=0.1)
    old = await _ip(db_session, subnet, "10.9.0.11", status="available", seen_days_ago=30)
    alloc = await _ip(db_session, subnet, "10.9.0.12", status="allocated", seen_days_ago=0.1)
    await db_session.commit()

    ids = {
        sid
        for sid, _, _ in await _matching_ip_free_but_responding_subjects(
            db_session, _rule(RULE_TYPE_IP_FREE_BUT_RESPONDING, 1)
        )
    }
    assert str(hit.id) in ids
    assert str(old.id) not in ids  # answered but not recently
    assert str(alloc.id) not in ids  # not 'available'


# ── stale reservation ───────────────────────────────────────────────────


async def test_stale_reservation(db_session: AsyncSession) -> None:
    subnet = await _make_subnet(db_session)
    stale_res = await _ip(db_session, subnet, "10.8.0.10", status="reserved", seen_days_ago=120)
    stale_static = await _ip(
        db_session, subnet, "10.8.0.11", status="static_dhcp", seen_days_ago=200
    )
    fresh = await _ip(db_session, subnet, "10.8.0.12", status="reserved", seen_days_ago=2)
    never = await _ip(db_session, subnet, "10.8.0.13", status="reserved", seen_days_ago=None)
    # allocated-stale is the OTHER rule's job (stale_ip_count) — must NOT match here.
    alloc = await _ip(db_session, subnet, "10.8.0.14", status="allocated", seen_days_ago=200)
    await db_session.commit()

    ids = {
        sid
        for sid, _, _ in await _matching_stale_reservation_subjects(
            db_session, _rule(RULE_TYPE_STALE_RESERVATION, 90)
        )
    }
    assert str(stale_res.id) in ids
    assert str(stale_static.id) in ids
    assert str(fresh.id) not in ids
    assert str(never.id) not in ids  # never seen → not high-confidence stale
    assert str(alloc.id) not in ids


# ── unknown MAC in static range ─────────────────────────────────────────


async def test_unknown_mac_in_static_range(db_session: AsyncSession) -> None:
    subnet = await _make_subnet(db_session)
    squatted = await _ip(
        db_session,
        subnet,
        "10.7.0.10",
        status="static_dhcp",
        seen_days_ago=0.1,
        mac="aa:bb:cc:00:00:01",
    )
    clean = await _ip(
        db_session,
        subnet,
        "10.7.0.11",
        status="reserved",
        seen_days_ago=0.1,
        mac="aa:bb:cc:00:00:02",
    )
    await db_session.flush()
    # squatted: a DIFFERENT MAC was observed recently → squat.
    db_session.add(IpMacHistory(ip_address_id=squatted.id, mac_address="de:ad:be:ef:00:99"))
    # clean: the SAME recorded MAC observed → not a squat.
    db_session.add(IpMacHistory(ip_address_id=clean.id, mac_address="aa:bb:cc:00:00:02"))
    await db_session.commit()

    ids = {
        sid
        for sid, _, _ in await _matching_unknown_mac_in_static_range_subjects(
            db_session, _rule(RULE_TYPE_UNKNOWN_MAC_IN_STATIC_RANGE, 7)
        )
    }
    assert str(squatted.id) in ids
    assert str(clean.id) not in ids


async def test_unknown_mac_ignores_old_observation(db_session: AsyncSession) -> None:
    """A differing MAC observed outside the recency window must not fire."""
    subnet = await _make_subnet(db_session)
    ip = await _ip(
        db_session,
        subnet,
        "10.6.0.10",
        status="static_dhcp",
        seen_days_ago=0.1,
        mac="aa:bb:cc:00:00:01",
    )
    await db_session.flush()
    db_session.add(
        IpMacHistory(
            ip_address_id=ip.id,
            mac_address="de:ad:be:ef:00:99",
            last_seen=datetime.now(UTC) - timedelta(days=30),
        )
    )
    await db_session.commit()

    ids = {
        sid
        for sid, _, _ in await _matching_unknown_mac_in_static_range_subjects(
            db_session, _rule(RULE_TYPE_UNKNOWN_MAC_IN_STATIC_RANGE, 7)
        )
    }
    assert str(ip.id) not in ids
