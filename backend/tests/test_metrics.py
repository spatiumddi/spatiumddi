"""Integration tests for the metrics time-series endpoints.

Exercises the control-plane read path against real DB rows. Agent-
side ingestion is covered in the agent test suites; here we only
care that the dashboard query aggregates correctly, picks sensible
bucket widths, and scopes by ``server_id`` when asked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dhcp import DHCPServer, DHCPServerGroup
from app.models.dns import DNSServer, DNSServerGroup
from app.models.metrics import DHCPMetricSample, DNSMetricSample


async def _make_user(db: AsyncSession) -> tuple[User, str]:
    user = User(
        username=f"user-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test",
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


async def _make_dhcp_server(db: AsyncSession) -> DHCPServer:
    g = DHCPServerGroup(name=f"g-{uuid.uuid4().hex[:8]}", description="")
    db.add(g)
    await db.flush()
    s = DHCPServer(
        name=f"s-{uuid.uuid4().hex[:8]}",
        host="127.0.0.1",
        driver="kea",
        server_group_id=g.id,
    )
    db.add(s)
    await db.flush()
    return s


@pytest.mark.asyncio
async def test_dns_timeseries_aggregates_buckets(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session)
    server = await _make_dns_server(db_session)

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    for i in range(3):
        db_session.add(
            DNSMetricSample(
                server_id=server.id,
                bucket_at=now - timedelta(minutes=i),
                queries_total=100 + i,
                noerror=90 + i,
                nxdomain=5,
                servfail=1,
                recursion=10,
            )
        )
    await db_session.commit()

    resp = await client.get(
        "/api/v1/metrics/dns/timeseries?window=1h",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window"] == "1h"
    assert body["bucket_seconds"] == 60
    assert len(body["points"]) == 3
    # Points are ordered ascending by bucket time.
    ts = [p["t"] for p in body["points"]]
    assert ts == sorted(ts)


@pytest.mark.asyncio
async def test_dns_timeseries_scopes_by_server_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session)
    s1 = await _make_dns_server(db_session)
    s2 = await _make_dns_server(db_session)

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    db_session.add(DNSMetricSample(server_id=s1.id, bucket_at=now, queries_total=50))
    db_session.add(DNSMetricSample(server_id=s2.id, bucket_at=now, queries_total=200))
    await db_session.commit()

    # All servers — sums across both.
    resp = await client.get(
        "/api/v1/metrics/dns/timeseries?window=1h",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.json()["points"][0]["queries_total"] == 250

    # Scoped — only s1.
    resp = await client.get(
        f"/api/v1/metrics/dns/timeseries?window=1h&server_id={s1.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.json()["points"][0]["queries_total"] == 50


@pytest.mark.asyncio
async def test_dhcp_timeseries_returns_empty_when_no_data(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session)
    resp = await client.get(
        "/api/v1/metrics/dhcp/timeseries?window=24h",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["points"] == []
    assert body["bucket_seconds"] == 300


@pytest.mark.asyncio
async def test_7d_window_uses_5min_buckets(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    await _make_dhcp_server(db_session)
    resp = await client.get(
        "/api/v1/metrics/dhcp/timeseries?window=7d",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["bucket_seconds"] == 300


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("window", "expected_bucket", "max_points"),
    [
        ("1h", 60, 60),
        ("6h", 60, 360),
        # 24 h is the crossover (#942). At raw 60 s it is 1,440 points
        # into a ~1,300 px chart — more points than pixels, which paints
        # a low-rate series as a solid band instead of a line.
        ("24h", 300, 288),
        ("7d", 300, 2016),
    ],
)
async def test_window_bucketing_keeps_points_plottable(
    client: AsyncClient,
    db_session: AsyncSession,
    window: str,
    expected_bucket: int,
    max_points: int,
) -> None:
    """Every window must resolve to a bucket that keeps it under a
    screen's worth of points. Guards both directions: shrinking a bucket
    reintroduces the smear, growing one throws away shape."""
    _, token = await _make_user(db_session)
    resp = await client.get(
        f"/api/v1/metrics/dns/timeseries?window={window}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bucket_seconds"] == expected_bucket
    from app.api.v1.metrics.router import WINDOW_SECONDS

    assert WINDOW_SECONDS[window] // body["bucket_seconds"] == max_points


@pytest.mark.asyncio
async def test_invalid_window_rejected(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session)
    resp = await client.get(
        "/api/v1/metrics/dns/timeseries?window=forever",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_dhcp_agent_metrics_ingest_roundtrip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST → dedupe on (server_id, bucket_at) → read back via /timeseries.

    We skip the real agent auth path for brevity — the critical
    invariant is that a repeat POST overwrites instead of duplicating.
    """
    _, token = await _make_user(db_session)
    server = await _make_dhcp_server(db_session)
    now = datetime.now(UTC).replace(second=0, microsecond=0)

    # Insert directly (agent-auth happy-path is covered elsewhere):
    # two writes to the same bucket with different values — the second
    # must overwrite, not duplicate, so the read-back shows 7/5, not
    # (3/2) + (7/5).
    db_session.add(
        DHCPMetricSample(
            server_id=server.id,
            bucket_at=now,
            discover=3,
            offer=3,
            request=3,
            ack=2,
        )
    )
    await db_session.commit()
    existing = await db_session.get(DHCPMetricSample, (server.id, now))
    existing.discover = 7
    existing.ack = 5
    await db_session.commit()

    resp = await client.get(
        "/api/v1/metrics/dhcp/timeseries?window=1h",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert len(body["points"]) == 1
    assert body["points"][0]["discover"] == 7
    assert body["points"][0]["ack"] == 5


@pytest.mark.asyncio
async def test_partial_bucket_reports_the_seconds_it_actually_covers(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The newest 5-minute bucket has only had one or two 60 s samples
    reported into it (#942).

    Dividing that partial sum by a full 300 renders a 70%+ phantom dip at
    the right-hand edge of every chart, on every refresh — measured on the
    dev estate as 0.027 against a steady 0.093. ``covered_seconds`` is what
    makes the rate honest, so it must reflect distinct agent buckets, not
    the nominal bucket width.
    """
    _, token = await _make_user(db_session)
    server = await _make_dns_server(db_session)

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    # A complete 5-minute bucket, then a partial one holding a single
    # sample. date_bin anchors on 2000-01-01, so :00 and :05 are boundaries.
    base = now.replace(minute=(now.minute // 10) * 10) - timedelta(minutes=20)
    for i in range(5):
        db_session.add(
            DNSMetricSample(
                server_id=server.id,
                bucket_at=base + timedelta(minutes=i),
                queries_total=10,
            )
        )
    db_session.add(
        DNSMetricSample(
            server_id=server.id,
            bucket_at=base + timedelta(minutes=5),
            queries_total=10,
        )
    )
    await db_session.commit()

    body = (
        await client.get(
            "/api/v1/metrics/dns/timeseries?window=24h",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    assert body["bucket_seconds"] == 300
    points = {p["t"]: p for p in body["points"]}
    full, partial = sorted(points.values(), key=lambda p: p["t"])[:2]

    assert full["covered_seconds"] == 300
    assert partial["covered_seconds"] == 60
    # The rate the chart draws. Same underlying qps; without the fix the
    # partial point would render at a fifth of it.
    assert full["queries_total"] / full["covered_seconds"] == pytest.approx(
        partial["queries_total"] / partial["covered_seconds"]
    )


@pytest.mark.asyncio
async def test_covered_seconds_is_a_time_span_not_a_row_count(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two servers reporting the same minute cover 60 seconds, not 120.

    Counting rows would halve the aggregate rate on every multi-server
    deployment — i.e. on exactly the ones with enough traffic to look at.
    """
    _, token = await _make_user(db_session)
    s1 = await _make_dns_server(db_session)
    s2 = await _make_dns_server(db_session)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    for srv in (s1, s2):
        db_session.add(DNSMetricSample(server_id=srv.id, bucket_at=now, queries_total=30))
    await db_session.commit()

    body = (
        await client.get(
            "/api/v1/metrics/dns/timeseries?window=1h",
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    point = body["points"][0]
    assert point["queries_total"] == 60
    assert point["covered_seconds"] == 60
