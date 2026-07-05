"""Reverse IP -> BGP Looking Glass route lookup (issue #566 Phase 3).

Shared by the operator-facing ``GET /looking-glass/routes/for-ip`` endpoint
and the ``find_bgp_route_for_ip`` MCP tool — one implementation, two
callers, so the LPM-by-single-IP semantics can't drift between the two
surfaces.
"""

from __future__ import annotations

import ipaddress

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bgp_looking_glass import BGPLGRoute


async def best_route_for_ip(db: AsyncSession, ip: str) -> tuple[BGPLGRoute, int] | None:
    """Longest-prefix-match a single IP against the active learned RIB.

    Returns ``(best_route, alternate_paths_count)`` or ``None`` when ``ip``
    doesn't parse or nothing in the active RIB covers it. Deterministic
    tie-break: longest prefix wins; among ties prefer the best path, then
    order by peer id.
    """
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return None

    rows = (
        (
            await db.execute(
                select(BGPLGRoute).where(
                    BGPLGRoute.prefix.op(">>=")(str(ip_obj)),
                    BGPLGRoute.withdrawn_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    covering: list[tuple[ipaddress._BaseNetwork, BGPLGRoute]] = []
    for row in rows:
        try:
            net = ipaddress.ip_network(str(row.prefix), strict=False)
        except ValueError:
            continue
        if net.version != ip_obj.version or ip_obj not in net:
            continue
        covering.append((net, row))

    if not covering:
        return None

    covering.sort(key=lambda t: (-t[0].prefixlen, 0 if t[1].is_best else 1, str(t[1].peer_id)))
    return covering[0][1], len(covering) - 1


__all__ = ["best_route_for_ip"]
