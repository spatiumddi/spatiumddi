"""Per-pool DHCP occupancy — live, driver-agnostic (issue #339).

Occupancy is computed from the mirrored ``DHCPLease`` rows rather than a
per-driver statistics call, so it works identically for Kea and Windows
DHCP (both pull active leases into ``dhcp_lease``) with no agent-protocol
change. A dynamic pool's *assigned* count is the number of distinct
addresses inside ``[start_ip, end_ip]`` that are unavailable to a dynamic
client: active-lease IPs **union** in-pool static reservations. Reservations
are counted even when the reserved device is currently offline (no active
lease) — the address is still withheld from the dynamic set, so counting
leases alone under-reports exhaustion (#631). The union (not a sum) keeps a
reserved-and-currently-online device from being double-counted. *Total* is
the address count of the inclusive range.

This is the data source for both the ``find_dhcp_pool_occupancy`` MCP tool
and the ``dhcp_pool_exhaustion`` alert evaluator.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPLease, DHCPPool, DHCPStaticAssignment


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


def _ints_in_range(ips: Iterable[object], start: int, end: int) -> set[int]:
    """Distinct integer IPs from ``ips`` that fall inside ``[start, end]``.

    Malformed / wrong-family values are skipped rather than raising, so a bad
    row can't blow up occupancy.
    """
    out: set[int] = set()
    for ip in ips:
        try:
            value = int(ipaddress.ip_address(str(ip)))
        except (ValueError, TypeError):
            continue
        if start <= value <= end:
            out.add(value)
    return out


async def compute_pool_occupancy(db: AsyncSession, pool: DHCPPool) -> PoolOccupancy:
    """Return live occupancy for one pool.

    ``assigned`` is the count of distinct in-range addresses that are
    unavailable to a dynamic client: active-lease IPs union in-pool static
    reservations (#631). Reservations are soft-delete-filtered by the global
    ORM listener. Range membership is checked in Python (ints) rather than via
    an INET SQL cast so leases + reservations share one comparison path.
    """
    total = pool_total_addresses(pool.start_ip, pool.end_ip)
    if total == 0:
        # Malformed / inverted / mixed-family range — skip the DB query
        # entirely so a bad pool can't make occupancy blow up.
        return PoolOccupancy(assigned=0, total=0)
    start = int(ipaddress.ip_address(str(pool.start_ip)))
    end = int(ipaddress.ip_address(str(pool.end_ip)))
    lease_ips = (
        (
            await db.execute(
                select(DHCPLease.ip_address)
                .where(DHCPLease.scope_id == pool.scope_id)
                .where(DHCPLease.state == "active")
            )
        )
        .scalars()
        .all()
    )
    reservation_ips = (
        (
            await db.execute(
                select(DHCPStaticAssignment.ip_address).where(
                    DHCPStaticAssignment.scope_id == pool.scope_id
                )
            )
        )
        .scalars()
        .all()
    )
    occupied = _ints_in_range(lease_ips, start, end) | _ints_in_range(reservation_ips, start, end)
    return PoolOccupancy(assigned=len(occupied), total=total)


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
    lease_rows = (
        await db.execute(
            select(DHCPLease.scope_id, DHCPLease.ip_address)
            .where(DHCPLease.scope_id.in_(scope_ids))
            .where(DHCPLease.state == "active")
            .distinct()
        )
    ).all()
    # In-pool static reservations withhold their address from the dynamic set
    # too (#631). Soft-delete-filtered by the global ORM listener.
    reservation_rows = (
        await db.execute(
            select(DHCPStaticAssignment.scope_id, DHCPStaticAssignment.ip_address).where(
                DHCPStaticAssignment.scope_id.in_(scope_ids)
            )
        )
    ).all()
    # Bucket occupied IPs (as ints) into a per-scope set so a reserved-and-
    # currently-leased address is counted once, not twice.
    by_scope: dict[uuid.UUID, set[int]] = {}

    def _bucket(rows: Iterable[Any]) -> None:
        for scope_id, ip in rows:
            try:
                by_scope.setdefault(scope_id, set()).add(int(ipaddress.ip_address(str(ip))))
            except (ValueError, TypeError):
                continue

    _bucket(lease_rows)
    _bucket(reservation_rows)

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
