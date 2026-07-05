"""Reverse IP -> BGP Looking Glass route lookup (issue #566 Phase 3).

Shared by the operator-facing ``GET /looking-glass/routes/for-ip`` endpoint
and the ``find_bgp_route_for_ip`` MCP tool — one implementation, two
callers, so the LPM-by-single-IP semantics can't drift between the two
surfaces. The actual LPM + tie-break now lives in
``app.services.looking_glass.reachability.find_covering_routes`` (issue
#566 Phase 6 hoisted it out so the multicast reachability cross-reference
could share it too); this module keeps its own thin ``(route, alt_count)``
wrapper so its two existing callers don't need to change shape.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bgp_looking_glass import BGPLGRoute
from app.services.looking_glass.reachability import find_covering_routes


async def best_route_for_ip(db: AsyncSession, ip: str) -> tuple[BGPLGRoute, int] | None:
    """Longest-prefix-match a single IP against the active learned RIB.

    Returns ``(best_route, alternate_paths_count)`` or ``None`` when ``ip``
    doesn't parse or nothing in the active RIB covers it. Deterministic
    tie-break: longest prefix wins; among ties prefer the best path, then
    order by peer id.
    """
    routes = await find_covering_routes(db, ip)
    if not routes:
        return None
    return routes[0], len(routes) - 1


__all__ = ["best_route_for_ip"]
