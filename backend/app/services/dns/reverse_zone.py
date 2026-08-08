"""Reverse-zone auto-creation for subnets that have DNS assignment.

When a subnet is created with a DNS assignment (either directly via
``dns_zone_id``/``dns_group_id`` or via block/space inheritance in a future
revision), SpatiumDDI creates the corresponding reverse zone
(``*.in-addr.arpa.`` or ``*.ip6.arpa.``) in the assigned server group if one
does not already exist.

Keeping the logic in the service layer (rather than inside the IPAM router)
satisfies the "driver abstraction / thin router" non-negotiable from
``CLAUDE.md``.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.dns import DNSServerGroup, DNSZone
from app.models.ipam import Subnet

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.auth import User

logger = structlog.get_logger(__name__)

# #844 — log-dedupe for the cross-space refusal below. The ensure path runs
# per IP allocation, so an unfixed misconfig would otherwise emit one warning
# per allocation forever; warn once per (subnet, zone) pair per process, and
# demote repeats to debug. Log suppression only — never consulted for logic.
_cross_space_warned: set[tuple[str, str]] = set()


def cidrs_overlap(a: object, b: object) -> bool:
    """True when two CIDR strings are the same address family and overlap.

    #844 uses this instead of a bare space-id comparison: IPv4 reverse zones
    aggregate to /24 (``compute_reverse_zone_name``), so two NON-overlapping
    subnets in different spaces legitimately share one reverse zone — their
    PTR names are disjoint and nothing leaks. Only an actual CIDR overlap
    can fold two tenants' PTRs onto the same names.
    """
    try:
        na = ipaddress.ip_network(str(a), strict=False)
        nb = ipaddress.ip_network(str(b), strict=False)
    except (ValueError, TypeError):
        return False
    return na.version == nb.version and na.overlaps(nb)


def compute_reverse_zone_name(network: str) -> str:
    """Return the canonical reverse-zone FQDN (with trailing dot) for ``network``.

    Uses ``ipaddress.ip_network(...).reverse_pointer`` which always produces a
    properly aligned in-addr.arpa / ip6.arpa name for byte/nibble-aligned
    prefixes. For non-aligned IPv4 prefixes (e.g. /23) we fall back to the
    nearest enclosing octet boundary, which is the standard BIND convention for
    an "aggregated" reverse zone covering multiple smaller subnets.
    """
    net = ipaddress.ip_network(network, strict=False)
    if isinstance(net, ipaddress.IPv4Network):
        # Align to the next-smaller /8, /16, or /24 boundary.
        if net.prefixlen <= 8:
            aligned_prefix = 8
        elif net.prefixlen <= 16:
            aligned_prefix = 16
        elif net.prefixlen <= 24:
            aligned_prefix = 24
        else:
            aligned_prefix = 24  # zones at /24 cover sub-prefixes
        aligned = ipaddress.ip_network(f"{net.network_address}/{aligned_prefix}", strict=False)
        name = aligned.network_address.reverse_pointer
        # reverse_pointer for 10.0.0.0 returns "0.0.0.10.in-addr.arpa"
        # We need to drop leading octets outside the /aligned_prefix.
        octets_kept = aligned_prefix // 8
        parts = name.split(".")
        # first 4 entries are the 4 IPv4 octets in reverse
        reversed_octets = parts[:4]
        suffix = ".".join(parts[4:])  # "in-addr.arpa"
        keep = reversed_octets[4 - octets_kept :]
        fqdn = ".".join(keep + [suffix])
    else:
        # IPv6 — reverse_pointer already yields a full nibble-aligned name.
        # For prefixes that aren't nibble-aligned, round up to the next nibble.
        aligned_prefix = ((net.prefixlen + 3) // 4) * 4
        aligned = ipaddress.ip_network(f"{net.network_address}/{aligned_prefix}", strict=False)
        name = aligned.network_address.reverse_pointer
        nibbles_kept = aligned_prefix // 4
        parts = name.split(".")
        reversed_nibbles = parts[:32]
        suffix = ".".join(parts[32:])  # "ip6.arpa"
        keep = reversed_nibbles[32 - nibbles_kept :]
        fqdn = ".".join(keep + [suffix])
    return fqdn if fqdn.endswith(".") else fqdn + "."


async def ensure_reverse_zone_for_subnet(
    db: AsyncSession,
    subnet: Subnet,
    current_user: User | None,
    *,
    dns_group_id: uuid.UUID | None = None,
    dns_zone_id: uuid.UUID | None = None,
) -> DNSZone | None:
    """Create the matching reverse zone for ``subnet`` if one does not exist.

    Resolution of the server group:

    1. Explicit ``dns_group_id`` argument wins.
    2. Otherwise fall back to the subnet's ``dns_group_ids`` / ``dns_zone_id``
       fields when the IPAM model supports them (safe ``getattr`` — the fields
       were introduced in a parallel Wave 2 migration and may not yet exist).
    3. If no group can be resolved the call is a no-op and returns ``None``.

    The function is idempotent: if a reverse zone with the computed FQDN
    already exists in the resolved group it is returned unchanged.

    Writes an ``audit_log`` entry on newly-created zones.
    """
    # 1. Resolve the server group
    group_id = dns_group_id
    if group_id is None:
        # Direct subnet-level zone assignment (if the column exists)
        subnet_zone_id = getattr(subnet, "dns_zone_id", None) or dns_zone_id
        if subnet_zone_id:
            zone = await db.get(DNSZone, subnet_zone_id)
            if zone is not None:
                group_id = zone.group_id
    if group_id is None:
        subnet_groups = getattr(subnet, "dns_group_ids", None) or []
        if subnet_groups:
            try:
                group_id = uuid.UUID(str(subnet_groups[0]))
            except (ValueError, TypeError):
                group_id = None

    if group_id is None:
        logger.debug(
            "reverse_zone_skipped_no_group",
            subnet_id=str(subnet.id),
            network=str(subnet.network),
        )
        return None

    group = await db.get(DNSServerGroup, group_id)
    if group is None:
        logger.warning(
            "reverse_zone_group_missing",
            subnet_id=str(subnet.id),
            group_id=str(group_id),
        )
        return None

    # 2. Compute reverse FQDN
    try:
        reverse_name = compute_reverse_zone_name(str(subnet.network))
    except ValueError:
        logger.warning(
            "reverse_zone_compute_failed",
            subnet_id=str(subnet.id),
            network=str(subnet.network),
        )
        return None

    # 3. Idempotency — return any existing zone with this FQDN in this group
    existing_q = await db.execute(
        select(DNSZone).where(
            DNSZone.group_id == group.id,
            DNSZone.name == reverse_name,
        )
    )
    existing = existing_q.scalar_one_or_none()
    if existing is not None:
        # #844 — an OVERLAPPING CIDR in another IP space computes the same
        # reverse zone name, and the (group_id, view_id, name) unique
        # constraint means a second zone can't exist. Silently reusing the
        # other space's zone would merge two tenants' PTRs onto the same
        # names (cross-tenant hostname disclosure), so refuse: this subnet's
        # IPs simply get no PTR until the operator gives the overlapping
        # space its own DNS group. Non-overlapping subnets sharing an
        # aggregated /24 zone (even across spaces) keep working — their PTR
        # names are disjoint, so there is nothing to leak.
        if existing.linked_subnet_id is not None and existing.linked_subnet_id != subnet.id:
            linked = (
                await db.execute(
                    select(Subnet.space_id, Subnet.network).where(
                        Subnet.id == existing.linked_subnet_id
                    )
                )
            ).first()
            if linked is None:
                # Dangling link — the owning subnet was deleted but its zone
                # survived (linked_subnet_id is ondelete=SET NULL on hard
                # delete, but a stale id can linger). Re-link to the live
                # subnet so the zone is attributable again instead of
                # becoming permanently "shared with everyone".
                existing.linked_subnet_id = subnet.id
                await db.flush()
                logger.info(
                    "reverse_zone_relinked",
                    zone_id=str(existing.id),
                    subnet_id=str(subnet.id),
                    name=reverse_name,
                )
            elif linked[0] != subnet.space_id and cidrs_overlap(linked[1], subnet.network):
                key = (str(subnet.id), str(existing.id))
                log_fn = logger.debug if key in _cross_space_warned else logger.warning
                _cross_space_warned.add(key)
                log_fn(
                    "reverse_zone_cross_space_conflict",
                    subnet_id=str(subnet.id),
                    space_id=str(subnet.space_id),
                    zone_id=str(existing.id),
                    name=reverse_name,
                    linked_subnet_id=str(existing.linked_subnet_id),
                    note="reverse zone owned by an overlapping subnet in "
                    "another IP space; refusing to share it — use a separate "
                    "DNS server group per overlapping IP space (#844)",
                )
                return None
        logger.debug(
            "reverse_zone_already_exists",
            subnet_id=str(subnet.id),
            zone_id=str(existing.id),
            name=reverse_name,
        )
        return existing

    # 4. Create the reverse zone
    zone = DNSZone(
        group_id=group.id,
        name=reverse_name,
        zone_type="primary",
        kind="reverse",
        is_auto_generated=True,
        linked_subnet_id=subnet.id,
    )
    db.add(zone)
    await db.flush()

    db.add(
        AuditLog(
            user_id=current_user.id if current_user else None,
            user_display_name=(current_user.display_name if current_user else "system"),
            auth_source=current_user.auth_source if current_user else "system",
            action="create",
            resource_type="dns_zone",
            resource_id=str(zone.id),
            resource_display=f"{reverse_name} (auto-reverse for {subnet.network})",
            result="success",
            new_value={
                "auto_generated": True,
                "linked_subnet_id": str(subnet.id),
                "kind": "reverse",
                "group_id": str(group.id),
            },
        )
    )
    logger.info(
        "reverse_zone_auto_created",
        subnet_id=str(subnet.id),
        zone_id=str(zone.id),
        name=reverse_name,
        group_id=str(group.id),
        at=datetime.now(UTC).isoformat(),
    )
    return zone
