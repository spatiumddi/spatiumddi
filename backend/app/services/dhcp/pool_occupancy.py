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


__all__ = ["PoolOccupancy", "compute_pool_occupancy", "pool_total_addresses"]
