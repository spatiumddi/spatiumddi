"""Rogue IPv6 RA detection (#524): classification + upsert + alert matcher."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertRule
from app.models.dhcp import (
    DHCPServer,
    DHCPServerGroup,
    RAObservedRouter,
    RARouterAllowlist,
)
from app.services.alerts import RULE_TYPE_ROGUE_RA, _matching_rogue_ra_subjects
from app.services.dhcp.ra_detection import ObservedRA, classify_router, record_observations


async def _group_with_server(db: AsyncSession) -> tuple[DHCPServerGroup, DHCPServer]:
    g = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:6]}", description="")
    db.add(g)
    await db.flush()
    s = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:6]}", host="10.0.0.2", driver="kea", server_group_id=g.id
    )
    db.add(s)
    await db.flush()
    return g, s


async def test_classify_rogue_vs_allowlisted(db_session: AsyncSession) -> None:
    g, _ = await _group_with_server(db_session)
    db_session.add(RARouterAllowlist(group_id=g.id, source_ip="fe80::1", note="core router"))
    await db_session.commit()
    assert await classify_router(db_session, g.id, "fe80::1", None) == "expected"
    assert await classify_router(db_session, g.id, "fe80::bad", None) == "rogue"


async def test_classify_by_mac(db_session: AsyncSession) -> None:
    g, _ = await _group_with_server(db_session)
    db_session.add(RARouterAllowlist(group_id=g.id, source_mac="00:11:22:33:44:55", note="gw"))
    await db_session.commit()
    assert await classify_router(db_session, g.id, "fe80::99", "00:11:22:33:44:55") == "expected"


async def test_record_observations_upserts_and_dedupes(db_session: AsyncSession) -> None:
    g, server = await _group_with_server(db_session)
    await db_session.commit()
    counts = await record_observations(
        db_session,
        server,
        [
            ObservedRA(
                source_ip="fe80::abcd",
                source_mac="aa:bb:cc:dd:ee:ff",
                prefixes=["2001:db8::/64"],
                managed_flag=False,
                other_flag=True,
                router_lifetime=1800,
            )
        ],
    )
    assert counts["rogue"] == 1
    # Re-report bumps last_seen, no dup row. Identity is (group, source_ip,
    # source_mac); a real sniffer re-reports the same link-layer source MAC, so
    # the same router dedupes to one row.
    counts2 = await record_observations(
        db_session,
        server,
        [
            ObservedRA(
                source_ip="fe80::abcd",
                source_mac="aa:bb:cc:dd:ee:ff",
                prefixes=["2001:db8::/64"],
            )
        ],
    )
    assert counts2["rogue"] == 1
    rows = (
        (
            await db_session.execute(
                select(RAObservedRouter).where(RAObservedRouter.group_id == g.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].prefixes == ["2001:db8::/64"]


async def test_distinct_mac_same_ip_gets_two_rows(db_session: AsyncSession) -> None:
    """Two physically distinct routers sharing a link-local (fe80::1) must not
    collapse into one row — identity is (group, source_ip, source_mac)."""
    g, server = await _group_with_server(db_session)
    await db_session.commit()
    await record_observations(
        db_session,
        server,
        [
            ObservedRA(source_ip="fe80::1", source_mac="aa:aa:aa:aa:aa:aa"),
            ObservedRA(source_ip="fe80::1", source_mac="bb:bb:bb:bb:bb:bb"),
        ],
    )
    rows = (
        (
            await db_session.execute(
                select(RAObservedRouter).where(RAObservedRouter.group_id == g.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {str(r.source_mac) for r in rows} == {
        "aa:aa:aa:aa:aa:aa",
        "bb:bb:bb:bb:bb:bb",
    }


async def test_null_mac_is_its_own_bucket(db_session: AsyncSession) -> None:
    g, server = await _group_with_server(db_session)
    await db_session.commit()
    await record_observations(
        db_session,
        server,
        [
            ObservedRA(source_ip="fe80::1", source_mac=None),
            ObservedRA(source_ip="fe80::1", source_mac="aa:aa:aa:aa:aa:aa"),
        ],
    )
    rows = (
        (
            await db_session.execute(
                select(RAObservedRouter).where(RAObservedRouter.group_id == g.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2


async def test_allowlisted_mac_does_not_bless_different_mac(db_session: AsyncSession) -> None:
    """An allowlist entry pinning (ip, macA) blesses only macA — a different MAC
    on the same link-local IP (a genuine rogue sharing fe80::1) stays rogue."""
    g, _ = await _group_with_server(db_session)
    db_session.add(
        RARouterAllowlist(group_id=g.id, source_ip="fe80::1", source_mac="aa:aa:aa:aa:aa:aa")
    )
    await db_session.commit()
    # Same IP + the allowlisted MAC → expected.
    assert await classify_router(db_session, g.id, "fe80::1", "aa:aa:aa:aa:aa:aa") == "expected"
    # Same IP, different MAC → NOT blessed.
    assert await classify_router(db_session, g.id, "fe80::1", "bb:bb:bb:bb:bb:bb") == "rogue"


async def test_ip_only_allowlist_blesses_any_mac(db_session: AsyncSession) -> None:
    """An IP-only allowlist entry (no MAC pinned) still matches by IP — the
    operator's explicit choice."""
    g, _ = await _group_with_server(db_session)
    db_session.add(RARouterAllowlist(group_id=g.id, source_ip="fe80::1", note="ip-only"))
    await db_session.commit()
    assert await classify_router(db_session, g.id, "fe80::1", "cc:cc:cc:cc:cc:cc") == "expected"


async def test_record_observations_skips_groupless(db_session: AsyncSession) -> None:
    s = DHCPServer(name="lone", host="10.0.0.9", driver="kea", server_group_id=None)
    db_session.add(s)
    await db_session.commit()
    counts = await record_observations(db_session, s, [ObservedRA(source_ip="fe80::1")])
    assert counts["skipped"] == 1


async def test_rogue_ra_alert_matcher(db_session: AsyncSession) -> None:
    g, server = await _group_with_server(db_session)
    await db_session.commit()
    await record_observations(db_session, server, [ObservedRA(source_ip="fe80::dead")])
    rule = AlertRule(
        name="rogue-ra", rule_type=RULE_TYPE_ROGUE_RA, severity="warning", threshold_days=1
    )
    subjects = await _matching_rogue_ra_subjects(db_session, rule)
    assert len(subjects) == 1
    assert "fe80::dead" in subjects[0][1]


async def test_rogue_ra_matcher_ignores_old_and_acknowledged(db_session: AsyncSession) -> None:
    g, _ = await _group_with_server(db_session)
    db_session.add(
        RAObservedRouter(
            group_id=g.id,
            source_ip="fe80::ac",
            classification="acknowledged",
            last_seen_at=datetime.now(UTC),
        )
    )
    db_session.add(
        RAObservedRouter(
            group_id=g.id,
            source_ip="fe80::01d",
            classification="rogue",
            last_seen_at=datetime.now(UTC) - timedelta(days=10),
        )
    )
    await db_session.commit()
    rule = AlertRule(
        name="rogue-ra", rule_type=RULE_TYPE_ROGUE_RA, severity="warning", threshold_days=1
    )
    subjects = await _matching_rogue_ra_subjects(db_session, rule)
    assert subjects == []
