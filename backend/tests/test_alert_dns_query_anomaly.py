"""DNS query-anomaly alert rules + per-view analytics (#371).

Covers the two new matchers (NXDOMAIN-spike, query-rate-spike) against seeded
``dns_metric_sample`` data, their open/auto-resolve lifecycle through
``evaluate_all``, and the per-view breakdown on the analytics endpoint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.alerts import AlertEvent, AlertRule
from app.models.auth import User
from app.models.dns import DNSServer, DNSServerGroup
from app.models.logs import DNSQueryLogEntry
from app.models.metrics import DNSMetricSample
from app.services import alerts as alerts_svc
from app.services.alerts import (
    RULE_TYPE_DNS_NXDOMAIN_SPIKE,
    RULE_TYPE_DNS_QUERY_RATE_SPIKE,
    _matching_dns_nxdomain_spike_subjects,
    _matching_dns_query_rate_spike_subjects,
    evaluate_all,
)


async def _make_admin(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"dnsq-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="DNS QA Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_dns_server(db: AsyncSession) -> DNSServer:
    g = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:8]}", description="")
    db.add(g)
    await db.flush()
    s = DNSServer(
        name=f"s-{uuid.uuid4().hex[:8]}",
        host="127.0.0.1",
        port=53,
        driver="bind9",
        group_id=g.id,
        is_primary=True,
        is_enabled=True,
    )
    db.add(s)
    await db.flush()
    return s


async def _sample(
    db: AsyncSession,
    server_id: uuid.UUID,
    *,
    minutes_ago: float,
    queries: int,
    nxdomain: int = 0,
) -> None:
    db.add(
        DNSMetricSample(
            server_id=server_id,
            bucket_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
            queries_total=queries,
            noerror=max(0, queries - nxdomain),
            nxdomain=nxdomain,
            servfail=0,
            recursion=0,
        )
    )


# ── NXDOMAIN-spike matcher ──────────────────────────────────────────────


async def test_nxdomain_spike_matches_high_ratio(db_session: AsyncSession) -> None:
    server = await _make_dns_server(db_session)
    rule = AlertRule(
        name="nx",
        rule_type=RULE_TYPE_DNS_NXDOMAIN_SPIKE,
        severity="warning",
        threshold_percent=40,
        min_free_addresses=200,
        enabled=True,
    )
    db_session.add(rule)
    # 1000 queries, 600 NXDOMAIN = 60% ≥ 40% AND 600 ≥ 200 → match.
    await _sample(db_session, server.id, minutes_ago=2, queries=1000, nxdomain=600)
    await db_session.commit()

    subjects = await _matching_dns_nxdomain_spike_subjects(db_session, rule)
    ids = {sid for sid, _, _ in subjects}
    assert str(server.id) in ids


async def test_nxdomain_spike_low_traffic_guard(db_session: AsyncSession) -> None:
    """High ratio but low absolute count must not fire (low-traffic guard)."""
    server = await _make_dns_server(db_session)
    rule = AlertRule(
        name="nx",
        rule_type=RULE_TYPE_DNS_NXDOMAIN_SPIKE,
        severity="warning",
        threshold_percent=40,
        min_free_addresses=200,
        enabled=True,
    )
    db_session.add(rule)
    # 10 queries, 9 NXDOMAIN = 90% ratio but only 9 < 200 floor → no match.
    await _sample(db_session, server.id, minutes_ago=2, queries=10, nxdomain=9)
    await db_session.commit()

    subjects = await _matching_dns_nxdomain_spike_subjects(db_session, rule)
    assert str(server.id) not in {sid for sid, _, _ in subjects}


async def test_nxdomain_spike_below_ratio_no_match(db_session: AsyncSession) -> None:
    server = await _make_dns_server(db_session)
    rule = AlertRule(
        name="nx",
        rule_type=RULE_TYPE_DNS_NXDOMAIN_SPIKE,
        severity="warning",
        threshold_percent=40,
        min_free_addresses=200,
        enabled=True,
    )
    db_session.add(rule)
    # 1000 queries, 250 NXDOMAIN = 25% < 40% → no match (even though 250 ≥ 200).
    await _sample(db_session, server.id, minutes_ago=2, queries=1000, nxdomain=250)
    await db_session.commit()

    subjects = await _matching_dns_nxdomain_spike_subjects(db_session, rule)
    assert str(server.id) not in {sid for sid, _, _ in subjects}


# ── Query-rate-spike matcher ────────────────────────────────────────────


async def test_query_rate_spike_matches(db_session: AsyncSession) -> None:
    server = await _make_dns_server(db_session)
    rule = AlertRule(
        name="rate",
        rule_type=RULE_TYPE_DNS_QUERY_RATE_SPIKE,
        severity="warning",
        threshold_percent=200,  # current ≥ prior × 3
        min_free_addresses=1000,
        enabled=True,
    )
    db_session.add(rule)
    # Current window (0-15m): 5000 queries. Prior window (15-30m): 1000.
    # 5000 ≥ 1000×3 = 3000 AND 5000 ≥ 1000 floor → spike.
    await _sample(db_session, server.id, minutes_ago=5, queries=5000)
    await _sample(db_session, server.id, minutes_ago=20, queries=1000)
    await db_session.commit()

    subjects = await _matching_dns_query_rate_spike_subjects(db_session, rule)
    assert str(server.id) in {sid for sid, _, _ in subjects}


async def test_query_rate_flat_no_match(db_session: AsyncSession) -> None:
    server = await _make_dns_server(db_session)
    rule = AlertRule(
        name="rate",
        rule_type=RULE_TYPE_DNS_QUERY_RATE_SPIKE,
        severity="warning",
        threshold_percent=200,
        min_free_addresses=1000,
        enabled=True,
    )
    db_session.add(rule)
    # Flat: 2000 current vs 2000 prior — no spike (2000 < 2000×3).
    await _sample(db_session, server.id, minutes_ago=5, queries=2000)
    await _sample(db_session, server.id, minutes_ago=20, queries=2000)
    await db_session.commit()

    subjects = await _matching_dns_query_rate_spike_subjects(db_session, rule)
    assert str(server.id) not in {sid for sid, _, _ in subjects}


# ── End-to-end open + auto-resolve through evaluate_all ─────────────────


class _DeliverSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, rule, event, targets):  # type: ignore[no-untyped-def]
        self.calls += 1
        return (False, False, False)


async def test_nxdomain_spike_opens_then_autoresolves(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alerts_svc, "_deliver", _DeliverSpy())
    server = await _make_dns_server(db_session)
    rule = AlertRule(
        name="nx",
        rule_type=RULE_TYPE_DNS_NXDOMAIN_SPIKE,
        severity="warning",
        threshold_percent=40,
        min_free_addresses=200,
        enabled=True,
    )
    db_session.add(rule)
    await _sample(db_session, server.id, minutes_ago=2, queries=1000, nxdomain=600)
    await db_session.commit()

    # Tick 1: opens one event for the server.
    await evaluate_all(db_session)
    open_events = (
        (
            await db_session.execute(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id, AlertEvent.resolved_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(open_events) == 1
    assert open_events[0].subject_type == "dns_server"
    assert open_events[0].subject_id == str(server.id)

    # Drop the spike: wipe the samples so the next tick no longer matches.
    await db_session.execute(
        DNSMetricSample.__table__.delete().where(DNSMetricSample.server_id == server.id)
    )
    await db_session.commit()

    # Tick 2: subject no longer matches → event auto-resolves.
    await evaluate_all(db_session)
    still_open = (
        (
            await db_session.execute(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id, AlertEvent.resolved_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(still_open) == 0


# ── Per-view analytics breakdown ────────────────────────────────────────


async def test_analytics_returns_top_views(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_admin(db_session)
    server = await _make_dns_server(db_session)
    now = datetime.now(UTC)
    # 3 queries in "internal", 1 in "external".
    for view in ("internal", "internal", "internal", "external"):
        db_session.add(
            DNSQueryLogEntry(
                server_id=server.id,
                ts=now,
                qname="host.example.com",
                qtype="A",
                view=view,
                raw="x",
            )
        )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/logs/dns-queries/analytics",
        headers={"Authorization": f"Bearer {token}"},
        json={"server_id": str(server.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    views = {row["key"]: row["count"] for row in body["top_views"]}
    assert views.get("internal") == 3
    assert views.get("external") == 1
