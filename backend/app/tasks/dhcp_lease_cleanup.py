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
from sqlalchemy.orm import selectinload

from app.celery_app import celery_app
from app.db import task_session
from app.models.dhcp import DHCPLease
from app.models.ipam import IPAddress
from app.services.dhcp.lease_history import record_lease_history

logger = structlog.get_logger(__name__)

# How long past expires_at before we clean up. Covers small clock skew and
# the agent's lease-event batch interval.
EXPIRY_GRACE = timedelta(minutes=5)


async def _sweep() -> int:
    cutoff = datetime.now(UTC) - EXPIRY_GRACE
    async with task_session() as db:
        # Find any active-marked lease whose actual expiry passed the grace.
        res = await db.execute(
            select(DHCPLease).where(
                DHCPLease.state == "active",
                DHCPLease.expires_at.is_not(None),
                DHCPLease.expires_at < cutoff,
            )
        )
        cleaned = 0
        # Lazy-import the DDNS revoke to keep this task light when
        # DDNS is off. The helper itself no-ops when the subnet is not
        # ddns_enabled, so calling it unconditionally is cheap.
        from app.services.dns.ddns import revoke_ddns_for_lease  # noqa: PLC0415

        now_ts = datetime.now(UTC)
        for lease in res.scalars().all():
            # Stamp lease history before flipping state. ``expired`` is
            # the time-based sweep label (vs ``removed`` from the
            # pull-leases absence-delete branch) so consumers can tell
            # the two apart.
            record_lease_history(db, lease, lease_state="expired", expired_at=now_ts)
            lease.state = "expired"
            # Remove mirrored IPAM row if auto_from_lease.
            ipam_res = await db.execute(
                select(IPAddress)
                .where(
                    IPAddress.address == lease.ip_address,
                    IPAddress.auto_from_lease.is_(True),
                )
                .options(selectinload(IPAddress.subnet))
            )
            for row in ipam_res.scalars().all():
                subnet = row.subnet
                if subnet is not None:
                    try:
                        await revoke_ddns_for_lease(db, subnet=subnet, ipam_row=row)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "dhcp_lease_cleanup_ddns_revoke_failed",
                            ip=str(row.address),
                            error=str(exc),
                        )
                await db.delete(row)
                cleaned += 1
        await db.commit()
        return cleaned


@celery_app.task(name="app.tasks.dhcp_lease_cleanup.sweep_expired_leases")
def sweep_expired_leases() -> dict[str, int]:
    cleaned = asyncio.run(_sweep())
    logger.info("dhcp_lease_sweep_complete", ipam_rows_removed=cleaned)
    return {"ipam_rows_removed": cleaned}
