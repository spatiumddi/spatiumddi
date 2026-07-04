"""Sweep expired DHCP leases and their mirrored IPAM rows.

Lease events from the agent already mirror active leases into IPAM (status='dhcp',
auto_from_lease=True) and remove them on explicit expired/released events. But
agents can miss events (container restart, lease_cmds hook drop), so we also
run a periodic sweep: any lease whose ``expires_at < now - grace`` gets marked
expired and its IPAM row (if auto_from_lease) removed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.celery_app import celery_app
from app.db import task_session
from app.models.dhcp import DHCPLease, DHCPScope
from app.models.ipam import IPAddress, Subnet
from app.services.dhcp.lease_history import record_lease_history

logger = structlog.get_logger(__name__)

# How long past expires_at before we clean up. Covers small clock skew and
# the agent's lease-event batch interval.
EXPIRY_GRACE = timedelta(minutes=5)

# How long a lease may sit in ``expired`` before the row itself is hard-deleted.
# Agent-based (Kea) drivers have no absence-delete reconciler — pull_leases only
# runs for agentless drivers, and the agent's expired-event branch drops just
# the IPAM mirror — so expired lease rows would otherwise linger in the DHCP
# view forever (#478). Kept generous so a recent expiry stays visible for a day;
# lease *history* is permanent regardless, so deleting the live row loses
# nothing and a renewal just creates a fresh active row.
EXPIRED_DELETE_GRACE = timedelta(hours=24)


async def _load_subnet_cache(
    db: AsyncSession,
) -> list[tuple[uuid.UUID, ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    """Load ``(subnet_id, parsed_network)`` for every subnet ONCE per sweep.

    ``_resolve_lease_subnet_id`` used to run a full ``SELECT id, network
    FROM subnet`` for every stale lease when it lacked a scope backlink —
    O(stale_leases × subnets) round-trips + reparse per lease. Loading the
    list once (and pre-parsing each CIDR) keeps the longest-prefix fallback
    at a single query per sweep, mirroring the pull-leases path. Unparseable
    networks are dropped up front so the per-lease loop stays clean.
    """
    cache: list[tuple[uuid.UUID, ipaddress.IPv4Network | ipaddress.IPv6Network]] = []
    for sid, network in (await db.execute(select(Subnet.id, Subnet.network))).all():
        try:
            cache.append((sid, ipaddress.ip_network(str(network), strict=False)))
        except (ValueError, TypeError):
            continue
    return cache


async def _resolve_lease_subnet_id(
    db: AsyncSession,
    lease: DHCPLease,
    subnet_cache: (
        list[tuple[uuid.UUID, ipaddress.IPv4Network | ipaddress.IPv6Network]] | None
    ) = None,
) -> uuid.UUID | None:
    """Find the IPAM subnet that owns this lease's address.

    SpatiumDDI allows overlapping private ranges across IPSpaces/VRFs, so
    the same address (e.g. 10.0.0.50) can be a valid auto_from_lease
    mirror in multiple subnets. Scope the mirror cleanup to the lease's
    own subnet so expiring one subnet's lease can't drop another's
    mirror. Prefer the lease's scope FK (DHCPScope.subnet_id); fall back
    to longest-prefix match over ``subnet_cache`` when the lease has no
    scope backlink. The sweep passes a cache loaded ONCE per run; the
    single-lease delete path leaves it None and we load it on demand.
    """
    if lease.scope_id is not None:
        subnet_id = (
            await db.execute(select(DHCPScope.subnet_id).where(DHCPScope.id == lease.scope_id))
        ).scalar_one_or_none()
        if subnet_id is not None:
            return subnet_id

    try:
        addr = ipaddress.ip_address(str(lease.ip_address))
    except (ValueError, TypeError):
        return None
    if subnet_cache is None:
        subnet_cache = await _load_subnet_cache(db)
    best: tuple[int, uuid.UUID] | None = None
    for sid, net in subnet_cache:
        if addr in net and (best is None or net.prefixlen > best[0]):
            best = (net.prefixlen, sid)
    return best[1] if best else None


async def _sweep() -> tuple[int, int]:
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
        # Load the subnet list once for the longest-prefix fallback in
        # _resolve_lease_subnet_id instead of re-querying per stale lease.
        subnet_cache = await _load_subnet_cache(db)
        for lease in res.scalars().all():
            # Stamp lease history before flipping state. ``expired`` is
            # the time-based sweep label (vs ``removed`` from the
            # pull-leases absence-delete branch) so consumers can tell
            # the two apart.
            record_lease_history(db, lease, lease_state="expired", expired_at=now_ts)
            lease.state = "expired"
            # Remove the mirrored IPAM row if auto_from_lease — but only
            # within this lease's owning subnet. An address-only lookup
            # would delete same-address mirrors in other subnets too.
            subnet_id = await _resolve_lease_subnet_id(db, lease, subnet_cache)
            if subnet_id is None:
                continue
            ipam_res = await db.execute(
                select(IPAddress)
                .where(
                    IPAddress.subnet_id == subnet_id,
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

        # Second pass — hard-delete leases that have sat in ``expired`` past the
        # longer grace so they stop lingering in the DHCP view (#478). History
        # was already stamped when the lease flipped to expired, and the mirror
        # was dropped above / by the agent, so this only removes the dead row.
        delete_cutoff = datetime.now(UTC) - EXPIRED_DELETE_GRACE
        stale = await db.execute(
            select(DHCPLease).where(
                DHCPLease.state == "expired",
                DHCPLease.expires_at.is_not(None),
                DHCPLease.expires_at < delete_cutoff,
            )
        )
        deleted = 0
        for lease in stale.scalars().all():
            await db.delete(lease)
            deleted += 1

        await db.commit()
        return cleaned, deleted


@celery_app.task(name="app.tasks.dhcp_lease_cleanup.sweep_expired_leases")
def sweep_expired_leases() -> dict[str, int]:
    cleaned, deleted = asyncio.run(_sweep())
    logger.info(
        "dhcp_lease_sweep_complete",
        ipam_rows_removed=cleaned,
        expired_leases_deleted=deleted,
    )
    return {"ipam_rows_removed": cleaned, "expired_leases_deleted": deleted}
