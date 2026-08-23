"""Turn SpatiumDDI's stored metrics into InfluxDB points (issue #889).

Two shapes of source, which behave differently and are worth telling
apart when reading a dashboard built on this data:

**Counter deltas** (``dns_metric_sample`` / ``dhcp_metric_sample``) —
agent-reported, already bucketed at 60 s, timestamped at the bucket. The
pusher walks these forward from a high-water mark, so a point's
timestamp is when the traffic happened, not when it was exported.

**Point-in-time gauges** (subnet utilization, per-scope active leases) —
sampled here, at push time, from counters the application already
maintains. There is no historical backfill for these: the first point is
the moment the target was enabled. The 60 s agent bucket is the floor for
the deltas; for the gauges the floor is the target's own
``push_interval_seconds``.

Every function returns ``Point`` objects with the measurement name
*unprefixed*; ``push`` applies the target's ``measurement_prefix`` so the
prefix isn't baked into the collectors.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPLease, DHCPScope, DHCPServer, DHCPServerGroup
from app.models.dns import DNSServer
from app.models.ipam import IPSpace, Subnet
from app.models.metrics import DHCPMetricSample, DNSMetricSample
from app.services.influxdb.line_protocol import Point

# Per-source cap on rows drained in a single push. A newly-enabled target
# backfills the whole retention window in ``ceil(rows / MAX_ROWS)`` ticks
# rather than in one oversized POST.
MAX_ROWS_PER_PUSH = 5000

# How far back before the high-water mark to re-send. Agents can report a
# bucket late (a restart, a blocked heartbeat), and a strict ``>`` cursor
# would skip it permanently. Re-sending is free: line protocol overwrites
# a point with the same measurement + tags + timestamp.
REPUSH_OVERLAP_SECONDS = 300

MEASUREMENT_DNS = "dns_queries"
MEASUREMENT_DHCP = "dhcp_messages"
MEASUREMENT_SUBNET = "subnet_utilization"
MEASUREMENT_SCOPE_LEASES = "dhcp_scope_leases"


def _epoch(dt: datetime) -> int:
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())


def _lower_bound(watermark: datetime | None) -> datetime | None:
    if watermark is None:
        return None
    tz_aware = watermark if watermark.tzinfo else watermark.replace(tzinfo=UTC)
    return tz_aware - timedelta(seconds=REPUSH_OVERLAP_SECONDS)


async def collect_dns_points(
    db: AsyncSession, watermark: datetime | None
) -> tuple[list[Point], datetime | None]:
    """DNS counter deltas newer than ``watermark`` (minus the overlap).

    Returns the points plus the newest ``bucket_at`` seen, which becomes
    the target's next watermark — ``None`` when nothing was found, so the
    caller leaves the stored value alone.
    """
    stmt = (
        select(DNSMetricSample, DNSServer.name)
        .join(DNSServer, DNSServer.id == DNSMetricSample.server_id)
        .order_by(DNSMetricSample.bucket_at.asc())
        .limit(MAX_ROWS_PER_PUSH)
    )
    lower = _lower_bound(watermark)
    if lower is not None:
        stmt = stmt.where(DNSMetricSample.bucket_at > lower)

    points: list[Point] = []
    newest: datetime | None = None
    for sample, server_name in (await db.execute(stmt)).all():
        points.append(
            Point(
                measurement=MEASUREMENT_DNS,
                tags={"server": server_name or "", "server_id": str(sample.server_id)},
                fields={
                    "queries_total": int(sample.queries_total),
                    "noerror": int(sample.noerror),
                    "nxdomain": int(sample.nxdomain),
                    "servfail": int(sample.servfail),
                    "recursion": int(sample.recursion),
                    "rate_dropped": int(sample.rate_dropped),
                    "rate_slipped": int(sample.rate_slipped),
                },
                timestamp=_epoch(sample.bucket_at),
            )
        )
        if newest is None or sample.bucket_at > newest:
            newest = sample.bucket_at
    return points, newest


async def collect_dhcp_points(
    db: AsyncSession, watermark: datetime | None
) -> tuple[list[Point], datetime | None]:
    """DHCP message-count deltas newer than ``watermark`` (minus overlap)."""
    stmt = (
        select(DHCPMetricSample, DHCPServer.name)
        .join(DHCPServer, DHCPServer.id == DHCPMetricSample.server_id)
        .order_by(DHCPMetricSample.bucket_at.asc())
        .limit(MAX_ROWS_PER_PUSH)
    )
    lower = _lower_bound(watermark)
    if lower is not None:
        stmt = stmt.where(DHCPMetricSample.bucket_at > lower)

    points: list[Point] = []
    newest: datetime | None = None
    for sample, server_name in (await db.execute(stmt)).all():
        points.append(
            Point(
                measurement=MEASUREMENT_DHCP,
                tags={"server": server_name or "", "server_id": str(sample.server_id)},
                fields={
                    "discover": int(sample.discover),
                    "offer": int(sample.offer),
                    "request": int(sample.request),
                    "ack": int(sample.ack),
                    "nak": int(sample.nak),
                    "decline": int(sample.decline),
                    "release": int(sample.release),
                    "inform": int(sample.inform),
                },
                timestamp=_epoch(sample.bucket_at),
            )
        )
        if newest is None or sample.bucket_at > newest:
            newest = sample.bucket_at
    return points, newest


async def collect_subnet_utilization_points(db: AsyncSession, now: datetime) -> list[Point]:
    """Point-in-time utilization gauge per live subnet.

    Reads the already-maintained ``allocated_ips`` / ``total_ips``
    counters — the same numbers the IPAM grid and the #44 daily history
    show — so this adds a query, not a scan. ``percent`` is recomputed
    here rather than read from ``utilization_percent`` so the three
    fields on a point can never disagree with each other.
    """
    stmt = (
        select(Subnet, IPSpace.name)
        .join(IPSpace, IPSpace.id == Subnet.space_id)
        .where(Subnet.deleted_at.is_(None))
    )
    ts = _epoch(now)
    points: list[Point] = []
    for subnet, space_name in (await db.execute(stmt)).all():
        total = int(subnet.total_ips or 0)
        allocated = int(subnet.allocated_ips or 0)
        percent = (allocated / total * 100.0) if total > 0 else 0.0
        points.append(
            Point(
                measurement=MEASUREMENT_SUBNET,
                tags={
                    "subnet": str(subnet.network),
                    "subnet_id": str(subnet.id),
                    "space": space_name or "",
                },
                fields={
                    "allocated": allocated,
                    "total": total,
                    "percent": round(percent, 4),
                },
                timestamp=ts,
            )
        )
    return points


async def collect_dhcp_scope_lease_points(db: AsyncSession, now: datetime) -> list[Point]:
    """Active-lease count per live DHCP scope.

    Scopes with zero active leases are emitted as ``0`` rather than
    omitted: an absent series and an empty scope look identical on a
    graph, and "the scope went quiet" is exactly what an operator wants
    to see.
    """
    counts_stmt = (
        select(DHCPLease.scope_id, func.count())
        .where(DHCPLease.scope_id.is_not(None), DHCPLease.state == "active")
        .group_by(DHCPLease.scope_id)
    )
    counts: dict[uuid.UUID, int] = {
        row[0]: int(row[1]) for row in (await db.execute(counts_stmt)).all()
    }

    scopes_stmt = (
        select(DHCPScope, Subnet.network, DHCPServerGroup.name)
        .join(Subnet, Subnet.id == DHCPScope.subnet_id)
        .join(DHCPServerGroup, DHCPServerGroup.id == DHCPScope.group_id)
        .where(DHCPScope.deleted_at.is_(None))
    )
    ts = _epoch(now)
    points: list[Point] = []
    for scope, network, group_name in (await db.execute(scopes_stmt)).all():
        points.append(
            Point(
                measurement=MEASUREMENT_SCOPE_LEASES,
                tags={
                    "scope": scope.name or str(network),
                    "scope_id": str(scope.id),
                    "group": group_name or "",
                    "subnet": str(network),
                },
                fields={
                    "active_leases": counts.get(scope.id, 0),
                    "is_active": bool(scope.is_active),
                },
                timestamp=ts,
            )
        )
    return points


__all__ = [
    "MAX_ROWS_PER_PUSH",
    "MEASUREMENT_DHCP",
    "MEASUREMENT_DNS",
    "MEASUREMENT_SCOPE_LEASES",
    "MEASUREMENT_SUBNET",
    "REPUSH_OVERLAP_SECONDS",
    "collect_dhcp_points",
    "collect_dhcp_scope_lease_points",
    "collect_dns_points",
    "collect_subnet_utilization_points",
]
