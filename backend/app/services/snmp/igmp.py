"""SNMP IGMP-snooping populator — issue #126 Phase 3 Wave 1.

Walks ``IGMP-STD-MIB.igmpCacheTable`` (RFC 2933) on every network
device with ``poll_igmp_snooping=True`` and writes
:class:`MulticastMembership` rows tagged ``seen_via='igmp_snooping'``.

The table indexes by ``(igmpCacheAddress, igmpCacheIfIndex)`` and
carries one row per active group join the device's IGMP snooper
has observed. The actionable column is ``igmpCacheLastReporter``:
the IP of the consumer host whose last membership-report packet
arrived, i.e. the endpoint we want to mark as a consumer.

Match policy (Wave 1 — operator-curated converges with discovery):

* Group address must already exist as a ``MulticastGroup`` row
  somewhere on the platform. We don't auto-create stub groups
  yet — that needs a "default multicast space" decision the
  operator hasn't made (issue #126 Phase 3 follow-up). Discovered
  groups without a matching registry row are dropped silently
  with a debug log.
* Reporter IP must already exist as an ``IPAddress`` row in the
  same ``IPSpace`` as the device. Same rationale: auto-creating
  IPAM rows is gated on the existing ``device.auto_create_discovered``
  flag, but the IGMP populator runs independently of the ARP
  cross-reference and shouldn't surprise the operator with new
  IPAM endpoints.
* When both match, upsert a ``MulticastMembership`` with
  ``role='consumer'`` and stamp ``last_seen_at=now()``. The
  ``UNIQUE (group_id, ip_address_id, role)`` constraint makes
  this idempotent — re-running the walk just refreshes
  ``last_seen_at``.

Phase 4+ enhancements (deliberately deferred):

* Vendor-MIB walks (``CISCO-IGMP-SNOOPING-MIB`` for per-port
  membership) for switches that don't expose the standard table.
* Auto-create stub groups under a configured default
  ``IPSpace`` so the registry converges with what's actually on
  the wire.
* Reaper sweep: any membership row tagged
  ``seen_via='igmp_snooping'`` whose ``last_seen_at`` is older
  than 30 minutes gets pruned.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAddress, Subnet
from app.models.multicast import MulticastGroup, MulticastMembership
from app.services.snmp.oids import (
    OID_IGMP_CACHE_LAST_REPORTER,
    OID_IGMP_CACHE_STATUS,
    OID_IGMP_CACHE_UP_TIME,
)

if TYPE_CHECKING:
    from app.models.network import NetworkDevice

logger = structlog.get_logger(__name__)


# ── Public dataclass ────────────────────────────────────────────────


@dataclass(frozen=True)
class IGMPCacheRow:
    """One observed IGMP cache entry from a device.

    Mirrors the relevant columns of ``igmpCacheEntry`` (RFC 2933 §3).
    ``last_reporter_ip`` is the consumer endpoint we'll match
    against the IPAM table.
    """

    group_address: str  # multicast destination, e.g. "239.5.7.42"
    if_index: int  # local interface index — currently informational
    last_reporter_ip: str | None  # may be 0.0.0.0 if no reports seen
    up_time_seconds: int | None
    status: int | None  # 1=active, 2=notInService, etc per RowStatus


# ── Walker ──────────────────────────────────────────────────────────


def _suffix_after(oid_str: str, base: str) -> str | None:
    """Strip the column-OID prefix from a varbind name to expose the
    table index. Mirrors the helper in ``poller.py`` — duplicated
    here so the IGMP module stays self-contained."""
    if not oid_str.startswith(base + "."):
        return None
    return oid_str[len(base) + 1 :]


def _parse_igmp_cache_index(suffix: str) -> tuple[str, int] | None:
    """``igmpCacheTable`` index is ``IpAddress.Integer32`` —
    a 4-octet group address followed by the ifIndex. The suffix
    after the column OID is therefore five dotted integers:
    ``<a>.<b>.<c>.<d>.<ifIndex>``.

    Returns ``(group_addr, if_index)`` or ``None`` on parse error.
    """
    parts = suffix.split(".")
    if len(parts) < 5:
        return None
    try:
        octets = [int(p) for p in parts[:4]]
        if_index = int(parts[4])
    except ValueError:
        return None
    if not all(0 <= o <= 255 for o in octets):
        return None
    group_addr = ".".join(str(o) for o in octets)
    return group_addr, if_index


def _parse_value_to_ip(value: Any) -> str | None:
    """Turn a varbind value (typically a pysnmp ``IpAddress`` /
    ``OctetString``) into a dotted-quad string or ``None``."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "0.0.0.0":
        return None
    try:
        return str(ipaddress.IPv4Address(s))
    except (ValueError, ipaddress.AddressValueError):
        return None


async def walk_igmp_cache(device: NetworkDevice) -> list[IGMPCacheRow]:
    """Walk ``igmpCacheTable`` on the device.

    Returns one :class:`IGMPCacheRow` per (group, ifIndex) pair. The
    walker uses the same per-column bulk-walk pattern the rest of the
    poller does (one ``bulkWalkCmd`` per column, max-rep=10) — see
    ``services/snmp/poller.py:_walk_oids`` for the rationale.

    Defensive: if the device doesn't implement IGMP-STD-MIB at all
    (returns ``noSuchObject`` immediately), the walk yields zero
    rows. The caller treats an empty result as a clean
    "nothing-to-merge" pass rather than an error.
    """
    # Local import to avoid a cycle at module load and to mirror the
    # pattern walk_arp / walk_fdb use in poller.py.
    from app.services.snmp.poller import _walk_oids  # noqa: PLC0415

    rows: dict[tuple[str, int], dict[str, Any]] = {}

    try:
        async for oid_str, value in _walk_oids(
            device,
            [
                OID_IGMP_CACHE_LAST_REPORTER,
                OID_IGMP_CACHE_UP_TIME,
                OID_IGMP_CACHE_STATUS,
            ],
        ):
            for base, key in (
                (OID_IGMP_CACHE_LAST_REPORTER, "reporter"),
                (OID_IGMP_CACHE_UP_TIME, "up_time"),
                (OID_IGMP_CACHE_STATUS, "status"),
            ):
                suffix = _suffix_after(oid_str, base)
                if suffix is None:
                    continue
                parsed = _parse_igmp_cache_index(suffix)
                if parsed is None:
                    break
                group_addr, if_index = parsed
                rows.setdefault(
                    (group_addr, if_index),
                    {"group_address": group_addr, "if_index": if_index},
                )[key] = value
                break
    except Exception as exc:  # noqa: BLE001
        # noSuchObject / unimplemented MIB / transport error — return
        # empty rather than failing the whole device poll. The caller
        # records the exception in last_poll_error itself.
        logger.debug("igmp_walk_failed", device_id=str(device.id), error=str(exc))
        return []

    out: list[IGMPCacheRow] = []
    for fields in rows.values():
        reporter = _parse_value_to_ip(fields.get("reporter"))
        up_time_raw = fields.get("up_time")
        try:
            up_time = (
                int(str(up_time_raw)) // 100 if up_time_raw is not None else None
            )  # TimeTicks are 1/100s
        except ValueError:
            up_time = None
        try:
            status = int(str(fields.get("status"))) if fields.get("status") else None
        except ValueError:
            status = None
        out.append(
            IGMPCacheRow(
                group_address=fields["group_address"],
                if_index=fields["if_index"],
                last_reporter_ip=reporter,
                up_time_seconds=up_time,
                status=status,
            )
        )
    return out


# ── Cross-reference into MulticastMembership ────────────────────────


async def cross_reference_igmp_memberships(
    db: AsyncSession,
    device: NetworkDevice,
    igmp_rows: Iterable[IGMPCacheRow],
) -> dict[str, int]:
    """Match observed IGMP joins against the multicast registry.

    Counters returned: ``updated`` (existing membership row's
    ``last_seen_at`` refreshed), ``created`` (new membership row
    inserted), ``skipped_no_group`` (no matching MulticastGroup),
    ``skipped_no_ip`` (no matching IPAddress in this device's space).

    Idempotent — re-running with the same wire data only refreshes
    ``last_seen_at`` on the existing memberships.
    """
    counts = {
        "updated": 0,
        "created": 0,
        "skipped_no_group": 0,
        "skipped_no_ip": 0,
    }
    rows = [r for r in igmp_rows if r.last_reporter_ip]
    if not rows:
        return counts

    # 1. Resolve group addresses → MulticastGroup ids in one query.
    addrs = sorted({r.group_address for r in rows})
    group_rows = (
        await db.execute(
            select(MulticastGroup.id, MulticastGroup.address).where(
                MulticastGroup.address.in_(addrs)
            )
        )
    ).all()
    group_id_by_addr: dict[str, Any] = {str(addr): gid for gid, addr in group_rows}

    # 2. Resolve reporter IPs → IPAddress ids scoped to the device's
    # IPSpace. Same scoping the ARP cross-reference uses, so a
    # multi-tenant deployment doesn't accidentally tag IPs from a
    # different tenant's space.
    space_id = device.ip_space_id
    if space_id is None:
        # Device not bound to a space — nothing we can match, but
        # surface the skips so operators see why.
        for r in rows:
            counts[
                "skipped_no_group" if r.group_address not in group_id_by_addr else "skipped_no_ip"
            ] += 1
        return counts

    ip_strs = sorted({r.last_reporter_ip for r in rows if r.last_reporter_ip})
    ip_rows = (
        await db.execute(
            select(IPAddress.id, IPAddress.address)
            .join(Subnet, Subnet.id == IPAddress.subnet_id)
            .where(Subnet.space_id == space_id)
            .where(IPAddress.address.in_(ip_strs))
        )
    ).all()
    ip_id_by_addr: dict[str, Any] = {str(addr): ipid for ipid, addr in ip_rows}

    # 3. For each match, upsert a ``MulticastMembership`` row.
    now = datetime.now(UTC)
    for r in rows:
        group_id = group_id_by_addr.get(r.group_address)
        if group_id is None:
            counts["skipped_no_group"] += 1
            continue
        reporter_ip = r.last_reporter_ip
        if reporter_ip is None:
            counts["skipped_no_ip"] += 1
            continue
        ip_id = ip_id_by_addr.get(reporter_ip)
        if ip_id is None:
            counts["skipped_no_ip"] += 1
            continue

        existing = (
            await db.execute(
                select(MulticastMembership).where(
                    MulticastMembership.group_id == group_id,
                    MulticastMembership.ip_address_id == ip_id,
                    MulticastMembership.role == "consumer",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                MulticastMembership(
                    group_id=group_id,
                    ip_address_id=ip_id,
                    role="consumer",
                    seen_via="igmp_snooping",
                    last_seen_at=now,
                    notes=f"discovered via SNMP IGMP cache on {device.name}",
                )
            )
            counts["created"] += 1
        else:
            existing.last_seen_at = now
            # Promote the seen_via tag if we're observing a row that
            # was previously hand-typed. This lets operators see when
            # discovery validated their manual entry.
            if existing.seen_via == "manual":
                existing.seen_via = "igmp_snooping"
            counts["updated"] += 1

    await db.flush()
    return counts


__all__ = [
    "IGMPCacheRow",
    "cross_reference_igmp_memberships",
    "walk_igmp_cache",
]
