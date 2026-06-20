"""Shared helpers for the per-server DHCP stats surfaces (#195).

The REST endpoint (``GET /api/v1/dhcp/servers/{id}/stats``, rendered by the
modal Stats tab) and the ``find_dhcp_server_stats`` MCP tool both summarise a
single server's recent traffic. They share the window catalogue and the
active-lease count from here so the two surfaces can never silently diverge
(e.g. adding a ``"12h"`` window lights up both at once; a change to how an
"active" lease is counted updates both).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPLease

# range -> window length in seconds. Single source of truth for both the REST
# endpoint and the MCP tool. Validation indexes this dict (the key is never
# interpolated into SQL), so ``range`` carries no injection surface.
STATS_WINDOW_SECONDS: dict[str, int] = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
}

# range -> date_bin bucket width for the chart timeseries. Deliberately NOT the
# dashboard's metrics ``_bucket_seconds_for`` (which the platform-wide cards
# depend on): a per-server modal chart wants to stay under ~360 points at every
# range, so 1h=60 (60 pts), 6h=60 (360), 24h=300 (288), 7d=1800 (336).
STATS_BUCKET_SECONDS: dict[str, int] = {
    "1h": 60,
    "6h": 60,
    "24h": 300,
    "7d": 1800,
}


async def active_lease_count(db: AsyncSession, server_id: uuid.UUID) -> int:
    """Count distinct active-lease IPs reported by one server.

    ``distinct(ip_address)`` collapses any duplicate lease rows for the same
    address within THIS server's table. It does not dedup across HA peers —
    each peer is a separate ``dhcp_server`` row with its own lease set, so an
    address leased by an HA pair is counted once per peer by design.
    """
    return int(
        (
            await db.execute(
                select(func.count(func.distinct(DHCPLease.ip_address)))
                .where(DHCPLease.server_id == server_id)
                .where(DHCPLease.state == "active")
            )
        ).scalar_one()
        or 0
    )
