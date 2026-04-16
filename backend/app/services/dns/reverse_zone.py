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

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.auth import User
    from app.models.ipam import Subnet

logger = structlog.get_logger(__name__)


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
