"""BGP Looking Glass troubleshooting alert family (issue #566 Phase 5).

Unit tests over the six ``bgp_lg_*`` matchers (session grace window,
RPKI-invalid severity override, tracked-prefix origin mismatch —
exact vs strictly-more-specific, flap threshold + recency window,
missing-advertisement CIDR containment) plus one end-to-end
``evaluate_all`` smoke test confirming AlertEvent open/auto-resolve.

Local helper duplication (``_make_collector`` / ``_make_peer``) rather
than cross-importing from ``test_looking_glass.py`` /
``test_bgp_hijack_monitor.py`` — matches this repo's existing
per-file-helper convention.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertEvent, AlertRule
from app.models.asn import ASN
from app.models.bgp_looking_glass import BGPLGPeer, BGPLGRoute, LookingGlassCollector
from app.models.bgp_monitor import BGPTrackedPrefix
from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services import alerts as alerts_svc

pytestmark = pytest.mark.asyncio


# ── helpers ──────────────────────────────────────────────────────────────


async def _make_collector(db: AsyncSession, **kw: object) -> LookingGlassCollector:
    col = LookingGlassCollector(
        name=kw.pop("name", f"col-{uuid.uuid4().hex[:8]}"),
        agent_id=kw.pop("agent_id", uuid.uuid4().hex),
        agent_registered=True,
        status=kw.pop("status", "active"),
        **kw,
    )
    db.add(col)
    await db.flush()
    return col


async def _make_peer(db: AsyncSession, collector: LookingGlassCollector, **kw: object) -> BGPLGPeer:
    peer = BGPLGPeer(
        name=kw.pop("name", f"peer-{uuid.uuid4().hex[:8]}"),
        collector_id=collector.id,
        local_asn=kw.pop("local_asn", 65000),
        peer_asn=kw.pop("peer_asn", 65001),
        peer_address=kw.pop("peer_address", "192.0.2.1"),
        **kw,
    )
    db.add(peer)
    await db.flush()
    return peer


async def _make_route(db: AsyncSession, peer: BGPLGPeer, prefix: str, **kw: object) -> BGPLGRoute:
    route = BGPLGRoute(
        peer_id=peer.id,
        prefix=prefix,
        next_hop=kw.pop("next_hop", "192.0.2.1"),
        **kw,
    )
    db.add(route)
    await db.flush()
    return route


async def _asn(db: AsyncSession, number: int = 64500) -> ASN:
    row = ASN(number=number, name=f"AS{number}", kind="public", registry="arin")
    db.add(row)
    await db.flush()
    return row


async def _tracked(
    db: AsyncSession, asn: ASN, prefix: str, *, allowed: list[int] | None = None
) -> BGPTrackedPrefix:
    row = BGPTrackedPrefix(
        asn_id=asn.id,
        prefix=prefix,
        expected_origin_asn=int(asn.number),
        source="manual",
        enabled=True,
        allowed_origins=allowed or [],
    )
    db.add(row)
    await db.flush()
    return row


async def _make_subnet(db: AsyncSession, *, network: str, should_advertise: bool) -> Subnet:
    space = IPSpace(name=f"sp-{uuid.uuid4().hex[:6]}", description="")
    db.add(space)
    await db.flush()
    block = IPBlock(space_id=space.id, network="10.0.0.0/8", name="b")
    db.add(block)
    await db.flush()
    subnet = Subnet(
        space_id=space.id,
        block_id=block.id,
        network=network,
        name=f"sn-{uuid.uuid4().hex[:6]}",
        total_ips=254,
        bgp_should_advertise=should_advertise,
    )
    db.add(subnet)
    await db.flush()
    return subnet


async def _rule(db: AsyncSession, rule_type: str, **kw: object) -> AlertRule:
    rule = AlertRule(
        name=rule_type,
        rule_type=rule_type,
        severity="warning",
        enabled=True,
        notify_syslog=False,
        notify_webhook=False,
        notify_smtp=False,
        **kw,
    )
    db.add(rule)
    await db.flush()
    return rule


# ── bgp_lg_session_down ───────────────────────────────────────────────────


async def test_session_down_grace_window(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col, session_state="active")
    rule = SimpleNamespace()

    t0 = datetime.now(UTC)
    # First observation — stamps the watermark, doesn't fire yet.
    matches = await alerts_svc._matching_bgp_lg_session_down_subjects(db_session, rule, t0)
    assert matches == []
    assert peer.down_since == t0

    # Still within the grace window.
    matches = await alerts_svc._matching_bgp_lg_session_down_subjects(
        db_session, rule, t0 + timedelta(seconds=30)
    )
    assert matches == []

    # Past the grace window — fires.
    matches = await alerts_svc._matching_bgp_lg_session_down_subjects(
        db_session, rule, t0 + timedelta(minutes=3)
    )
    assert len(matches) == 1
    assert matches[0][0] == str(peer.id)

    # Re-established — watermark clears, no match.
    peer.session_state = "established"
    matches = await alerts_svc._matching_bgp_lg_session_down_subjects(
        db_session, rule, t0 + timedelta(minutes=4)
    )
    assert matches == []
    assert peer.down_since is None


# ── bgp_lg_rpki_invalid_route ──────────────────────────────────────────────


async def test_rpki_invalid_route_severity_critical(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    await _make_route(db_session, peer, "192.0.2.0/24", origin_asn=65001, rpki_status="valid")
    invalid = await _make_route(
        db_session, peer, "198.51.100.0/24", origin_asn=65002, rpki_status="invalid"
    )
    await db_session.commit()

    rule = SimpleNamespace()
    matches = await alerts_svc._matching_bgp_lg_rpki_invalid_route_subjects(db_session, rule)
    assert len(matches) == 1
    sid, _disp, _msg, severity = matches[0]
    assert sid == str(invalid.id)
    assert severity == "critical"


# ── bgp_lg_unexpected_origin ───────────────────────────────────────────────


async def test_unexpected_origin_requires_tracked_prefix(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    asn = await _asn(db_session, 64500)
    route = await _make_route(db_session, peer, "192.0.2.0/24", origin_asn=64999)
    await db_session.commit()

    rule = SimpleNamespace()

    # No tracked prefix at all → zero matches even with a suspicious route.
    matches = await alerts_svc._matching_bgp_lg_unexpected_origin_subjects(db_session, rule)
    assert matches == []

    # Tracked with a DIFFERENT expected origin → matches.
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")
    await db_session.commit()
    matches = await alerts_svc._matching_bgp_lg_unexpected_origin_subjects(db_session, rule)
    assert len(matches) == 1
    assert matches[0][0] == str(route.id)

    # Same origin as expected → no match.
    route.origin_asn = int(asn.number)
    await db_session.commit()
    matches = await alerts_svc._matching_bgp_lg_unexpected_origin_subjects(db_session, rule)
    assert matches == []

    # Origin explicitly allowlisted → no match.
    route.origin_asn = 64999
    tracked.allowed_origins = [64999]
    await db_session.commit()
    matches = await alerts_svc._matching_bgp_lg_unexpected_origin_subjects(db_session, rule)
    assert matches == []


# ── bgp_lg_more_specific ───────────────────────────────────────────────────


async def test_more_specific_strict_containment(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    asn = await _asn(db_session, 64500)
    await _tracked(db_session, asn, "10.0.0.0/16")

    # Exact match at the tracked prefix — NOT more_specific's job.
    exact = await _make_route(db_session, peer, "10.0.0.0/16", origin_asn=64999)
    # Strictly-contained sub-prefix with an unexpected origin — IS a match.
    sub = await _make_route(db_session, peer, "10.0.1.0/24", origin_asn=64999)
    await db_session.commit()

    rule = SimpleNamespace()
    matches = await alerts_svc._matching_bgp_lg_more_specific_subjects(db_session, rule)
    ids = {sid for sid, _disp, _msg in matches}
    assert str(sub.id) in ids
    assert str(exact.id) not in ids


# ── bgp_lg_route_flap ──────────────────────────────────────────────────────


async def test_route_flap_threshold_and_window(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    now = datetime.now(UTC)

    below_threshold = await _make_route(
        db_session, peer, "10.1.0.0/24", flap_count=1, last_flap_at=now
    )
    stale = await _make_route(
        db_session,
        peer,
        "10.2.0.0/24",
        flap_count=5,
        last_flap_at=now - timedelta(minutes=20),
    )
    recent = await _make_route(
        db_session,
        peer,
        "10.3.0.0/24",
        flap_count=5,
        last_flap_at=now - timedelta(minutes=2),
    )
    await db_session.commit()

    rule = SimpleNamespace(threshold_percent=3)
    matches = await alerts_svc._matching_bgp_lg_route_flap_subjects(db_session, rule, now)
    ids = {sid for sid, _disp, _msg in matches}
    assert ids == {str(recent.id)}
    assert str(below_threshold.id) not in ids
    assert str(stale.id) not in ids


# ── bgp_lg_missing_advertisement ───────────────────────────────────────────


async def test_missing_advertisement_covering_route(db_session: AsyncSession) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)

    flagged = await _make_subnet(db_session, network="10.5.0.0/24", should_advertise=True)
    await _make_subnet(db_session, network="10.6.0.0/24", should_advertise=False)
    await db_session.commit()

    rule = SimpleNamespace()

    # No covering route yet → matches.
    matches = await alerts_svc._matching_bgp_lg_missing_advertisement_subjects(db_session, rule)
    ids = {sid for sid, _disp, _msg in matches}
    assert str(flagged.id) in ids

    # A covering (supernet-or-equal) active route appears → no longer matches.
    covering = await _make_route(db_session, peer, "10.5.0.0/16")
    await db_session.commit()
    matches = await alerts_svc._matching_bgp_lg_missing_advertisement_subjects(db_session, rule)
    ids = {sid for sid, _disp, _msg in matches}
    assert str(flagged.id) not in ids

    # Route withdraws → matches again.
    covering.withdrawn_at = datetime.now(UTC)
    await db_session.commit()
    matches = await alerts_svc._matching_bgp_lg_missing_advertisement_subjects(db_session, rule)
    ids = {sid for sid, _disp, _msg in matches}
    assert str(flagged.id) in ids


# ── end-to-end evaluate_all() — open + auto-resolve ────────────────────────


async def test_evaluate_all_opens_and_autoresolves_rpki_invalid(
    db_session: AsyncSession,
) -> None:
    col = await _make_collector(db_session)
    peer = await _make_peer(db_session, col)
    route = await _make_route(
        db_session, peer, "203.0.113.0/24", origin_asn=64999, rpki_status="invalid"
    )
    rule = await _rule(db_session, alerts_svc.RULE_TYPE_BGP_LG_RPKI_INVALID_ROUTE)
    await db_session.commit()

    summary = await alerts_svc.evaluate_all(db_session)
    assert summary["opened"] >= 1
    events = (
        (await db_session.execute(select(AlertEvent).where(AlertEvent.rule_id == rule.id)))
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].subject_type == "bgp_lg_route"
    assert events[0].severity == "critical"
    assert events[0].resolved_at is None

    # Route withdraws → the matcher stops returning it → auto-resolve.
    route.withdrawn_at = datetime.now(UTC)
    await db_session.commit()
    await alerts_svc.evaluate_all(db_session)
    event = (
        (await db_session.execute(select(AlertEvent).where(AlertEvent.rule_id == rule.id)))
        .scalars()
        .one()
    )
    assert event.resolved_at is not None
