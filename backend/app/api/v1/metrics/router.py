"""Time-series read endpoints for the built-in dashboard.

Two symmetric endpoints — ``/metrics/dns/timeseries`` and
``/metrics/dhcp/timeseries`` — return bucketed per-server counter
deltas over a requested time window. The backing rows are written by
the agents (see `app.api.v1.{dns,dhcp}.agents.agent_metrics`); this
module is read-only.

Window → bucket selection is auto-scaled so charts stay readable:
short windows (≤ 24 h) return raw 60 s rows, longer windows aggregate
server-side into 5 min buckets. That keeps 7-day charts under 2k
points without losing shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.models.metrics import DHCPMetricSample, DNSMetricSample

router = APIRouter()


WINDOW_SECONDS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
}


def _bucket_seconds_for(window: str) -> int:
    """Pick an aggregation bucket that keeps charts under ~2k points."""
    if WINDOW_SECONDS[window] > 24 * 3600:
        return 300  # 5 min
    return 60


def _window_start(window: str) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=WINDOW_SECONDS[window])


class DNSTimePoint(BaseModel):
    t: datetime
    queries_total: int
    noerror: int
    nxdomain: int
    servfail: int
    recursion: int


class DNSTimeseries(BaseModel):
    window: str
    bucket_seconds: int
    points: list[DNSTimePoint]


class DHCPTimePoint(BaseModel):
    t: datetime
    discover: int
    offer: int
    request: int
    ack: int
    nak: int
    decline: int
    release: int
    inform: int


class DHCPTimeseries(BaseModel):
    window: str
    bucket_seconds: int
    points: list[DHCPTimePoint]


def _validate_window(window: str) -> None:
    if window not in WINDOW_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"window must be one of {sorted(WINDOW_SECONDS)}",
        )


@router.get("/dns/timeseries", response_model=DNSTimeseries)
async def dns_timeseries(
    db: DB,
    _: CurrentUser,
    server_id: uuid.UUID | None = Query(None),
    window: str = Query("24h"),
) -> DNSTimeseries:
    _validate_window(window)
    bucket_s = _bucket_seconds_for(window)
    since = _window_start(window)

    # Use date_bin(interval, ts, anchor) so aggregated points land on
    # stable bucket boundaries across requests. Sum across all
    # servers when ``server_id`` isn't pinned — gives one aggregate
    # series per metric for the dashboard overview card.
    bucket_col = func.date_bin(
        timedelta(seconds=bucket_s),
        DNSMetricSample.bucket_at,
        datetime(2000, 1, 1, tzinfo=UTC),
    ).label("t")

    stmt = select(
        bucket_col,
        func.sum(DNSMetricSample.queries_total).label("queries_total"),
        func.sum(DNSMetricSample.noerror).label("noerror"),
        func.sum(DNSMetricSample.nxdomain).label("nxdomain"),
        func.sum(DNSMetricSample.servfail).label("servfail"),
        func.sum(DNSMetricSample.recursion).label("recursion"),
    ).where(DNSMetricSample.bucket_at >= since)
    if server_id is not None:
        stmt = stmt.where(DNSMetricSample.server_id == server_id)
    stmt = stmt.group_by(bucket_col).order_by(bucket_col)

    rows = (await db.execute(stmt)).all()
    points = [
        DNSTimePoint(
            t=row._mapping["t"],
            queries_total=int(row.queries_total or 0),
            noerror=int(row.noerror or 0),
            nxdomain=int(row.nxdomain or 0),
            servfail=int(row.servfail or 0),
            recursion=int(row.recursion or 0),
        )
        for row in rows
    ]
    return DNSTimeseries(window=window, bucket_seconds=bucket_s, points=points)


@router.get("/dhcp/timeseries", response_model=DHCPTimeseries)
async def dhcp_timeseries(
    db: DB,
    _: CurrentUser,
    server_id: uuid.UUID | None = Query(None),
    window: str = Query("24h"),
) -> DHCPTimeseries:
    _validate_window(window)
    bucket_s = _bucket_seconds_for(window)
    since = _window_start(window)

    bucket_col = func.date_bin(
        timedelta(seconds=bucket_s),
        DHCPMetricSample.bucket_at,
        datetime(2000, 1, 1, tzinfo=UTC),
    ).label("t")

    stmt = select(
        bucket_col,
        func.sum(DHCPMetricSample.discover).label("discover"),
        func.sum(DHCPMetricSample.offer).label("offer"),
        func.sum(DHCPMetricSample.request).label("request"),
        func.sum(DHCPMetricSample.ack).label("ack"),
        func.sum(DHCPMetricSample.nak).label("nak"),
        func.sum(DHCPMetricSample.decline).label("decline"),
        func.sum(DHCPMetricSample.release).label("release"),
        func.sum(DHCPMetricSample.inform).label("inform"),
    ).where(DHCPMetricSample.bucket_at >= since)
    if server_id is not None:
        stmt = stmt.where(DHCPMetricSample.server_id == server_id)
    stmt = stmt.group_by(bucket_col).order_by(bucket_col)

    rows = (await db.execute(stmt)).all()
    points = [
        DHCPTimePoint(
            t=row._mapping["t"],
            discover=int(row.discover or 0),
            offer=int(row.offer or 0),
            request=int(row.request or 0),
            ack=int(row.ack or 0),
            nak=int(row.nak or 0),
            decline=int(row.decline or 0),
            release=int(row.release or 0),
            inform=int(row.inform or 0),
        )
        for row in rows
    ]
    return DHCPTimeseries(window=window, bucket_seconds=bucket_s, points=points)
