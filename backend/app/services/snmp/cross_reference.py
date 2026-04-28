"""Cross-reference SNMP-derived ARP entries back into IPAM.

After a successful ARP poll, every (ip, mac) pair we observed gets:

  * ``IPAddress.last_seen_at`` set to ``now()`` if it falls inside a
    known subnet of the device's bound ``IPSpace``.
  * ``IPAddress.last_seen_method`` set to ``"snmp"``.
  * ``IPAddress.mac_address`` populated *only* when it's currently
    ``NULL`` — operator data is never overwritten.
  * Optional auto-create: when ``device.auto_create_discovered=True``
    AND the IP isn't already in the IPAM table AND it falls inside a
    known subnet, insert a fresh row with ``status='discovered'``.

Returns a small counters dict so the Celery task can stamp something
human-readable on the audit row + ``last_poll_*_count`` columns.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAddress, Subnet

if TYPE_CHECKING:
    from app.models.network import NetworkDevice

    from .poller import ArpData

logger = structlog.get_logger(__name__)


async def cross_reference_arp(
    db: AsyncSession,
    device: NetworkDevice,
    arp_entries: Iterable[ArpData],
) -> dict[str, int]:
    """Reconcile ARP results against IPAM rows in the device's space.

    Idempotent — running it twice with the same wire data produces no
    extra writes (just refreshes ``last_seen_at`` to "now"). Auto-
    create is gated on ``device.auto_create_discovered`` so the
    default is "log only, never invent rows."
    """
    counts = {"updated": 0, "created": 0, "skipped_no_subnet": 0}
    arp_list = list(arp_entries)
    if not arp_list:
        return counts

    # Snapshot the device's subnet list once and keep it in IP-network
    # form. We do an O(n*m) containment test below — m is "subnets in
    # the space" which is bounded; n is the ARP table which is bounded
    # too. No need for an interval tree at this size.
    subnet_rows = list(
        (await db.execute(select(Subnet).where(Subnet.space_id == device.ip_space_id)))
        .scalars()
        .all()
    )
    subnet_nets: list[tuple[Subnet, ipaddress.IPv4Network | ipaddress.IPv6Network]] = []
    for s in subnet_rows:
        try:
            subnet_nets.append((s, ipaddress.ip_network(str(s.network), strict=False)))
        except ValueError:
            continue

    now = datetime.now(UTC)

    # Pre-fetch every IPAddress row that appears in this ARP batch and
    # belongs to one of the device's subnets — avoids N+1 lookups.
    if subnet_rows:
        ip_strs = sorted({arp.ip_address for arp in arp_list})
        existing_rows = list(
            (
                await db.execute(
                    select(IPAddress)
                    .where(IPAddress.subnet_id.in_([s.id for s in subnet_rows]))
                    .where(IPAddress.address.in_(ip_strs))
                )
            )
            .scalars()
            .all()
        )
    else:
        existing_rows = []

    by_ip: dict[str, IPAddress] = {str(r.address): r for r in existing_rows}

    for arp in arp_list:
        ip_str = arp.ip_address
        existing = by_ip.get(ip_str)

        if existing is not None:
            existing.last_seen_at = now
            existing.last_seen_method = "snmp"
            if existing.mac_address is None and arp.mac_address:
                existing.mac_address = arp.mac_address
            counts["updated"] += 1
            continue

        # No row yet — locate the matching subnet (if any).
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            counts["skipped_no_subnet"] += 1
            continue

        matched_subnet: Subnet | None = None
        for subnet, net in subnet_nets:
            if ip_obj.version != net.version:
                continue
            if ip_obj in net:
                matched_subnet = subnet
                break

        if matched_subnet is None:
            counts["skipped_no_subnet"] += 1
            continue

        if not device.auto_create_discovered:
            # Subnet exists but operator hasn't opted in to auto-create.
            counts["skipped_no_subnet"] += 1
            continue

        new_row = IPAddress(
            subnet_id=matched_subnet.id,
            address=ip_str,
            status="discovered",
            mac_address=arp.mac_address,
            last_seen_at=now,
            last_seen_method="snmp",
        )
        db.add(new_row)
        counts["created"] += 1

    return counts


__all__ = ["cross_reference_arp"]
