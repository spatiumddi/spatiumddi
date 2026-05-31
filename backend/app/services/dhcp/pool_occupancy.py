"""Per-pool DHCP occupancy — live, driver-agnostic (issue #339).

Occupancy is computed from the mirrored ``DHCPLease`` rows rather than a
per-driver statistics call, so it works identically for Kea and Windows
DHCP (both pull active leases into ``dhcp_lease``) with no agent-protocol
change. A dynamic pool's *assigned* count is the number of distinct
active-lease IPs that fall inside ``[start_ip, end_ip]`` for the pool's
scope (DISTINCT dedupes the two rows an HA pair reports for one lease);
*total* is the address count of the inclusive range.

This is the data source for both the ``find_dhcp_pool_occupancy`` MCP tool
and the ``dhcp_pool_exhaustion`` alert evaluator.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPLease, DHCPPool


@dataclass(frozen=True)
class PoolOccupancy:
    assigned: int
    total: int

    @property
    def free(self) -> int:
        return max(0, self.total - self.assigned)

    @property
    def percent(self) -> float:
        """Occupancy as a 0..100 float; 0.0 for a zero-size pool."""
        return (self.assigned / self.total * 100.0) if self.total > 0 else 0.0


def pool_total_addresses(start_ip: str, end_ip: str) -> int:
    """Inclusive address count of ``[start_ip, end_ip]``.

    Returns 0 if the range is malformed or inverted (start > end) or the two
    ends are different families, so a bad pool can't make occupancy blow up.
    """
    try:
        start = ipaddress.ip_address(str(start_ip))
        end = ipaddress.ip_address(str(end_ip))
    except ValueError:
        return 0
    if start.version != end.version:
        return 0
    n = int(end) - int(start) + 1
    return n if n > 0 else 0


async def compute_pool_occupancy(db: AsyncSession, pool: DHCPPool) -> PoolOccupancy:
    """Return live occupancy for one pool.

    Counts distinct active-lease IPs inside the pool range for the pool's
    scope. ``ip_address`` is INET, so the range comparison is cast
    explicitly (asyncpg otherwise binds the params as VARCHAR and Postgres
    rejects the inet operators — same cast the voice-lease alert uses).
    """
    total = pool_total_addresses(pool.start_ip, pool.end_ip)
    if total == 0:
        # Malformed / inverted / mixed-family range — skip the DB range query
        # entirely. Running it could match unrelated rows (inet ordering across
        # families) and hand back a non-zero ``assigned`` for a 0-size pool,
        # so a bad pool can't make occupancy blow up.
        return PoolOccupancy(assigned=0, total=0)
    assigned = (
        await db.execute(
            select(func.count(func.distinct(DHCPLease.ip_address)))
            .where(DHCPLease.scope_id == pool.scope_id)
            .where(DHCPLease.state == "active")
            .where(
                text(
                    "ip_address >= CAST(:start AS inet) AND ip_address <= CAST(:end AS inet)"
                ).bindparams(start=str(pool.start_ip), end=str(pool.end_ip))
            )
        )
    ).scalar_one()
    return PoolOccupancy(assigned=int(assigned or 0), total=total)


async def compute_pool_occupancy_batch(
    db: AsyncSession, pools: Sequence[DHCPPool]
) -> dict[uuid.UUID, PoolOccupancy]:
    """Occupancy for many pools in a single lease query (no N+1).

    Fetches the distinct active-lease ``(scope_id, ip)`` pairs for every
    scope the pools live in with one query, then buckets them into pool
    ranges in memory. Use this in hot paths (the 60s alert tick, the MCP
    tool) instead of calling :func:`compute_pool_occupancy` per pool.
    """
    result: dict[uuid.UUID, PoolOccupancy] = {}
    totals: dict[uuid.UUID, int] = {}
    valid: list[DHCPPool] = []
    for pool in pools:
        total = pool_total_addresses(pool.start_ip, pool.end_ip)
        totals[pool.id] = total
        if total == 0:
            result[pool.id] = PoolOccupancy(assigned=0, total=0)
        else:
            valid.append(pool)
    if not valid:
        return result

    scope_ids = {p.scope_id for p in valid}
    rows = (
        await db.execute(
            select(DHCPLease.scope_id, DHCPLease.ip_address)
            .where(DHCPLease.scope_id.in_(scope_ids))
            .where(DHCPLease.state == "active")
            .distinct()
        )
    ).all()
    # Bucket distinct lease IPs (as ints) by scope.
    by_scope: dict[uuid.UUID, list[int]] = {}
    for scope_id, ip in rows:
        try:
            by_scope.setdefault(scope_id, []).append(int(ipaddress.ip_address(str(ip))))
        except ValueError:
            continue

    for pool in valid:
        start = int(ipaddress.ip_address(str(pool.start_ip)))
        end = int(ipaddress.ip_address(str(pool.end_ip)))
        assigned = sum(1 for v in by_scope.get(pool.scope_id, ()) if start <= v <= end)
        result[pool.id] = PoolOccupancy(assigned=assigned, total=totals[pool.id])
    return result


__all__ = [
    "PoolOccupancy",
    "compute_pool_occupancy",
    "compute_pool_occupancy_batch",
    "pool_total_addresses",
]
