"""Sweep expired DHCP leases and their mirrored IPAM rows.

Lease events from the agent already mirror active leases into IPAM (status='dhcp',
auto_from_lease=True) and remove them on explicit expired/released events. But
agents can miss events (container restart, lease_cmds hook drop), so we also
run a periodic sweep: any lease whose ``expires_at < now - grace`` gets marked
expired and its IPAM row (if auto_from_lease) removed.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from app.celery_app import celery_app
from app.db import AsyncSessionLocal
from app.models.dhcp import DHCPLease
from app.models.ipam import IPAddress

logger = structlog.get_logger(__name__)

# How long past expires_at before we clean up. Covers small clock skew and
# the agent's lease-event batch interval.
EXPIRY_GRACE = timedelta(minutes=5)


async def _sweep() -> int:
    cutoff = datetime.now(UTC) - EXPIRY_GRACE
    async with AsyncSessionLocal() as db:
        # Find any active-marked lease whose actual expiry passed the grace.
        res = await db.execute(
            select(DHCPLease).where(
                DHCPLease.state == "active",
                DHCPLease.expires_at.is_not(None),
                DHCPLease.expires_at < cutoff,
            )
        )
        cleaned = 0
        for lease in res.scalars().all():
            lease.state = "expired"
            # Remove mirrored IPAM row if auto_from_lease.
            ipam_res = await db.execute(
                select(IPAddress).where(
                    IPAddress.address == lease.ip_address,
                    IPAddress.auto_from_lease.is_(True),
                )
            )
            for row in ipam_res.scalars().all():
                await db.delete(row)
                cleaned += 1
        await db.commit()
        return cleaned


@celery_app.task(name="app.tasks.dhcp_lease_cleanup.sweep_expired_leases")
def sweep_expired_leases() -> dict[str, int]:
    cleaned = asyncio.run(_sweep())
    logger.info("dhcp_lease_sweep_complete", ipam_rows_removed=cleaned)
    return {"ipam_rows_removed": cleaned}
