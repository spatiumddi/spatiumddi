"""IPAM API — IP spaces, blocks, subnets, and addresses."""

import ipaddress
import re
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentUser
from app.api.v1.ipam.io_router import router as io_router
from app.core.permissions import require_any_resource_permission
from app.drivers.dhcp import is_agentless
from app.models.audit import AuditLog
from app.models.dhcp import DHCPConfigOp, DHCPScope, DHCPServer, DHCPStaticAssignment
from app.models.dns import DNSRecord, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet, SubnetDomain
from app.models.vlans import VLAN
from app.services.dhcp.config_bundle import build_config_bundle
from app.services.dhcp.windows_writethrough import (
    push_scope_delete,
    push_statics_bulk_delete,
)

logger = structlog.get_logger(__name__)

# Router-level permission gate: GET → `read`, POST/PUT/PATCH → `write`,
# DELETE → `delete`. Endpoints under /ipam manipulate IPAM resources, so we
# accept any of the four IPAM resource types (an "IPAM Editor" role grants all
# four; a scoped Subnet-writer role would be matched here for subnet routes
# and fail for space routes — which is intended). Per-row scoping happens
# inline in the handlers via `user_has_permission`.
router = APIRouter(
    dependencies=[
        Depends(
            require_any_resource_permission(
                "ip_space", "ip_block", "subnet", "ip_address", "custom_field"
            )
        )
    ]
)
router.include_router(io_router)

# ── Internal helpers ───────────────────────────────────────────────────────────


def _parse_network(network: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Parse and validate a CIDR string. Raises ValueError on bad input."""
    try:
        return ipaddress.ip_network(network, strict=False)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid CIDR notation: {network}",
        )


_BIGINT_MAX = 2**63 - 1


def _total_ips(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> int:
    """Usable host count (excludes network/broadcast for IPv4 prefixlen < 31).

    IPv6 subnets can be up to 2^64 addresses (a /64), which overflows the
    BIGINT column backing ``Subnet.total_ips``. We clamp at BIGINT max —
    utilization_percent is effectively always 0 for a /64 regardless.
    """
    if isinstance(net, ipaddress.IPv6Network):
        # IPv6 has no broadcast. The network address is conventionally reserved
        # in many stacks (anycast subnet-router) but still addressable, so we
        # keep the full count and clamp to BIGINT.
        return min(net.num_addresses, _BIGINT_MAX)
    if net.prefixlen >= 31:
        return net.num_addresses
    return net.num_addresses - 2


async def _assert_no_overlap(
    db: AsyncSession,
    space_id: uuid.UUID,
    network: str,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Raise 409 if the given network overlaps with any existing subnet in the space."""
    q = (
        "SELECT network FROM subnet "
        "WHERE space_id = CAST(:space_id AS uuid) AND network && CAST(:network AS cidr)"
    )
    params: dict[str, Any] = {"space_id": str(space_id), "network": network}
    if exclude_id:
        q += " AND id != CAST(:exclude_id AS uuid)"
        params["exclude_id"] = str(exclude_id)
    result = await db.execute(text(q), params)
    row = result.fetchone()
    if row:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Network {network} overlaps with existing subnet {row[0]}",
        )


async def _assert_no_block_overlap(
    db: AsyncSession,
    space_id: uuid.UUID,
    network: str,
    parent_block_id: uuid.UUID | None,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Raise 409 if the proposed block overlaps (or exactly duplicates) any
    existing sibling block — i.e. another block in the same space at the
    same level of the tree (top-level, or sharing ``parent_block_id``).

    Parent/child relationships across levels are intentionally allowed: a
    child explicitly declaring its parent is expected to be contained within
    that parent. The sibling-only check catches the common mistake of
    duplicating a block at the same level without triggering false positives
    for a legitimate reparent / nesting.
    """
    q = (
        "SELECT network FROM ip_block "
        "WHERE space_id = CAST(:space_id AS uuid) "
        "AND network && CAST(:network AS cidr)"
    )
    params: dict[str, Any] = {"space_id": str(space_id), "network": network}
    if parent_block_id is None:
        q += " AND parent_block_id IS NULL"
    else:
        q += " AND parent_block_id = CAST(:parent_id AS uuid)"
        params["parent_id"] = str(parent_block_id)
    if exclude_id:
        q += " AND id != CAST(:exclude_id AS uuid)"
        params["exclude_id"] = str(exclude_id)
    row = (await db.execute(text(q), params)).fetchone()
    if row:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Block {network} overlaps with existing block {row[0]}",
        )


async def _update_utilization(db: AsyncSession, subnet_id: uuid.UUID) -> None:
    """Recompute and persist allocated_ips and utilization_percent for a subnet."""
    allocated = (
        await db.scalar(
            select(func.count())
            .select_from(IPAddress)
            .where(IPAddress.subnet_id == subnet_id)
            .where(IPAddress.status != "available")
        )
        or 0
    )

    subnet = await db.get(Subnet, subnet_id)
    if subnet:
        subnet.allocated_ips = allocated
        subnet.utilization_percent = (
            round(allocated / subnet.total_ips * 100, 2) if subnet.total_ips > 0 else 0.0
        )


async def _update_block_utilization(db: AsyncSession, block_id: uuid.UUID) -> None:
    """Recompute utilization_percent for a block by summing allocated IPs across all
    descendant subnets (recursive), expressed as a fraction of the block's CIDR size.
    Also updates all ancestor blocks up the tree.
    """
    block = await db.get(IPBlock, block_id)
    if block is None:
        return

    # Sum allocated_ips for all subnets in this block and all descendant blocks
    result = await db.execute(
        text("""
            WITH RECURSIVE descendants AS (
                SELECT id FROM ip_block WHERE id = CAST(:block_id AS uuid)
                UNION ALL
                SELECT b.id FROM ip_block b
                    INNER JOIN descendants d ON b.parent_block_id = d.id
            )
            SELECT COALESCE(SUM(s.allocated_ips), 0)
            FROM subnet s
            WHERE s.block_id IN (SELECT id FROM descendants)
        """),
        {"block_id": str(block_id)},
    )
    allocated = result.scalar() or 0

    net = ipaddress.ip_network(str(block.network), strict=False)
    block_total = net.num_addresses
    block.utilization_percent = (
        round(float(allocated) / block_total * 100, 2) if block_total > 0 else 0.0
    )

    # Walk up the tree and update each ancestor
    if block.parent_block_id:
        await _update_block_utilization(db, block.parent_block_id)


async def _resolve_effective_dns(
    db: AsyncSession, subnet: Subnet
) -> tuple[list[str], uuid.UUID | None, list[str]]:
    """Return ``(dns_group_ids, dns_zone_id, dns_additional_zone_ids)`` for a
    subnet, walking subnet → block ancestors → space.

    Every caller that needs to route DNS ops for a subnet MUST go through
    this helper — reading ``subnet.dns_group_ids`` / ``subnet.dns_zone_id``
    directly ignores the ``dns_inherit_settings`` toggle and will keep
    pushing to the previously-assigned server after the operator has
    flipped the subnet back to inherit (real bug, not hypothetical).

    Semantics mirror ``GET /subnets/{id}/effective-dns`` — that HTTP
    endpoint is the UI's source of truth; the two used to drift when
    this was a per-level ad-hoc walk.
    """
    # Subnet override wins if inherit is off.
    if not subnet.dns_inherit_settings:
        zone_id = uuid.UUID(subnet.dns_zone_id) if subnet.dns_zone_id else None
        return (
            list(subnet.dns_group_ids or []),
            zone_id,
            list(subnet.dns_additional_zone_ids or []),
        )
    # Walk up the block chain.
    current = await db.get(IPBlock, subnet.block_id) if subnet.block_id else None
    while current is not None:
        if not current.dns_inherit_settings:
            zone_id = uuid.UUID(current.dns_zone_id) if current.dns_zone_id else None
            return (
                list(current.dns_group_ids or []),
                zone_id,
                list(current.dns_additional_zone_ids or []),
            )
        if current.parent_block_id:
            current = await db.get(IPBlock, current.parent_block_id)
        else:
            # Reached the root block — fall through to the space. The
            # space has no ``inherit`` flag; it's always the root.
            space = await db.get(IPSpace, current.space_id)
            if space is None:
                break
            zone_id = uuid.UUID(space.dns_zone_id) if space.dns_zone_id else None
            return (
                list(space.dns_group_ids or []),
                zone_id,
                list(space.dns_additional_zone_ids or []),
            )
    return ([], None, [])


async def _resolve_effective_zone(db: AsyncSession, subnet: Subnet) -> uuid.UUID | None:
    """Return the effective forward DNS zone UUID for a subnet."""
    _, zone_id, _ = await _resolve_effective_dns(db, subnet)
    return zone_id


# ── Assignment collision warnings ─────────────────────────────────────────────
#
# Non-fatal guardrails on IP create / update. Two kinds of collision:
#
#   1. FQDN  — same ``(lower(hostname), forward_zone_id)`` on another IP.
#              Often accidental (two people naming a host "web"); occasionally
#              deliberate (round-robin A records). Warn + let user confirm.
#   2. MAC   — same MAC address on another IP in any subnet. Usually means
#              the MAC was cloned / moved; the old row should be decommissioned
#              before re-use.
#
# Both are *warnings*, not errors. If the client re-submits with ``force=True``
# the write proceeds.

_MAC_DELIMS = re.compile(r"[:\-.\s]")


def _normalize_mac(raw: str | None) -> str | None:
    """Canonicalize a user-entered MAC to 12 lowercase hex chars, or None.

    Accepts ``aa:bb:cc:dd:ee:ff``, ``aa-bb-cc-dd-ee-ff``,
    ``aabb.ccdd.eeff``, or bare ``aabbccddeeff``. Returns ``None`` when the
    input is missing or not 12 hex chars — the caller skips the MAC check
    and lets the DB layer surface any hard error at insert time.
    """
    if not raw:
        return None
    cleaned = _MAC_DELIMS.sub("", raw.strip()).lower()
    if len(cleaned) != 12 or not all(c in "0123456789abcdef" for c in cleaned):
        return None
    return cleaned


async def _check_ip_collisions(
    db: AsyncSession,
    *,
    hostname: str | None,
    forward_zone_id: uuid.UUID | None,
    mac_address: str | None,
    exclude_ip_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Return FQDN + MAC collision warnings for a pending IP assignment.

    - FQDN check runs only when both ``hostname`` and ``forward_zone_id``
      resolve — nothing to collide on otherwise.
    - MAC check runs only when a well-formed MAC is supplied; Postgres's
      MACADDR comparison normalizes canonical forms automatically, but we
      still prefilter malformed input so the query doesn't error out.
    - ``exclude_ip_id`` is set on update so the IP doesn't collide with
      its own current state.
    """
    warnings: list[dict[str, Any]] = []

    if hostname and forward_zone_id:
        host_lower = hostname.strip().lower()
        q = (
            select(IPAddress, DNSZone.name, Subnet.network)
            .join(DNSZone, DNSZone.id == IPAddress.forward_zone_id)
            .join(Subnet, Subnet.id == IPAddress.subnet_id)
            .where(func.lower(IPAddress.hostname) == host_lower)
            .where(IPAddress.forward_zone_id == forward_zone_id)
        )
        if exclude_ip_id is not None:
            q = q.where(IPAddress.id != exclude_ip_id)
        for ip, zone_name, subnet_network in (await db.execute(q)).all():
            warnings.append(
                {
                    "kind": "fqdn_collision",
                    "fqdn": f"{ip.hostname}.{zone_name.rstrip('.')}",
                    "existing_ip": str(ip.address),
                    "existing_subnet": str(subnet_network),
                    "existing_ip_id": str(ip.id),
                }
            )

    mac_norm = _normalize_mac(mac_address)
    if mac_norm and mac_address is not None:
        q = (
            select(IPAddress, Subnet.network)
            .join(Subnet, Subnet.id == IPAddress.subnet_id)
            .where(IPAddress.mac_address == mac_address)
        )
        if exclude_ip_id is not None:
            q = q.where(IPAddress.id != exclude_ip_id)
        for ip, subnet_network in (await db.execute(q)).all():
            warnings.append(
                {
                    "kind": "mac_collision",
                    "mac_address": str(ip.mac_address),
                    "existing_ip": str(ip.address),
                    "existing_hostname": ip.hostname,
                    "existing_subnet": str(subnet_network),
                    "existing_ip_id": str(ip.id),
                }
            )

    return warnings


def _collision_http_exc(warnings: list[dict[str, Any]]) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"warnings": warnings, "requires_confirmation": True},
    )


async def _resolve_reverse_zone(
    db: AsyncSession, subnet: Subnet, ip_addr: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> DNSZone | None:
    """Find the reverse zone covering this IP. Prefers a zone linked to the
    subnet; falls back to any reverse zone in the subnet's *effective* DNS
    group whose name is a suffix of the IP's reverse_pointer.

    "Effective" is load-bearing here — see ``_resolve_effective_dns``.
    """
    rev_pointer = ip_addr.reverse_pointer + "."
    # 1. Subnet-linked reverse zone
    res = await db.execute(
        select(DNSZone).where(
            DNSZone.linked_subnet_id == subnet.id,
            DNSZone.kind == "reverse",
        )
    )
    z = res.scalar_one_or_none()
    if z and rev_pointer.endswith("." + z.name.rstrip(".") + "."):
        return z
    # 2. Walk effective DNS group(s) for the subnet — inheritance-aware.
    effective_group_ids, _, _ = await _resolve_effective_dns(db, subnet)
    if not effective_group_ids:
        return None
    res = await db.execute(
        select(DNSZone).where(
            DNSZone.group_id.in_(effective_group_ids),
            DNSZone.kind == "reverse",
        )
    )
    candidates = list(res.scalars().all())
    # Choose the longest matching suffix (most specific)
    best: DNSZone | None = None
    for z in candidates:
        zname = z.name.rstrip(".") + "."
        if rev_pointer.endswith("." + zname) or rev_pointer == zname:
            if best is None or len(z.name) > len(best.name):
                best = z
    return best


async def _enqueue_dns_op(
    db: AsyncSession, zone: DNSZone, op: str, name: str, rtype: str, value: str, ttl: int | None
) -> None:
    """Wrapper to enqueue a record op against the zone's primary server.
    Imported lazily to avoid circular import."""
    from app.services.dns.record_ops import enqueue_record_op
    from app.services.dns.serial import bump_zone_serial

    target_serial = bump_zone_serial(zone)
    await enqueue_record_op(
        db,
        zone,
        op,
        {"name": name, "type": rtype, "value": value, "ttl": ttl},
        target_serial=target_serial,
    )


async def _create_alias_records(
    db: AsyncSession,
    ip: IPAddress,
    subnet: Subnet,
    aliases: list[Any],
    zone_id: uuid.UUID | None = None,
) -> None:
    """Create user-specified alias DNS records tied to this IP.

    Aliases are marked ``auto_generated=True`` + ``ip_address_id=ip.id`` so
    the existing delete-path in ``_sync_dns_record(action='delete')`` cleans
    them up automatically when the IP is purged.

    Value inference:
      - CNAME → points to the IP's FQDN (<hostname>.<zone>).
      - A     → points to the IP itself (secondary name → same IP).
    """
    if not aliases or not ip.hostname:
        return
    effective_zone_id = zone_id or await _resolve_effective_zone(db, subnet)
    if not effective_zone_id:
        return
    zone = await db.get(DNSZone, effective_zone_id)
    if zone is None:
        return
    zone_domain = zone.name.rstrip(".")
    primary_fqdn = f"{ip.hostname}.{zone_domain}."
    # Pick the correct default for secondary-A aliases based on the IP family.
    try:
        addr_obj = ipaddress.ip_address(str(ip.address))
    except ValueError:
        addr_obj = None
    is_v6 = isinstance(addr_obj, ipaddress.IPv6Address)
    for al in aliases:
        rtype = (getattr(al, "record_type", None) or al.get("record_type") or "CNAME").upper()
        # Callers using "A" on an IPv6 primary really mean AAAA; normalise.
        if rtype == "A" and is_v6:
            rtype = "AAAA"
        name = (getattr(al, "name", None) or al.get("name") or "").strip().rstrip(".")
        if not name or rtype not in {"CNAME", "A", "AAAA"}:
            continue
        # Skip if a conflicting record already exists for (zone, name, type)
        dup = await db.execute(
            select(DNSRecord).where(
                DNSRecord.zone_id == effective_zone_id,
                DNSRecord.name == name,
                DNSRecord.record_type == rtype,
            )
        )
        if dup.scalar_one_or_none():
            continue
        value = primary_fqdn if rtype == "CNAME" else str(ip.address)
        rec = DNSRecord(
            zone_id=effective_zone_id,
            name=name,
            fqdn=f"{name}.{zone_domain}",
            record_type=rtype,
            value=value,
            auto_generated=True,
            ip_address_id=ip.id,
            created_by_user_id=ip.created_by_user_id,
        )
        db.add(rec)
        await db.flush()
        await _enqueue_dns_op(db, zone, "create", name, rtype, value, None)


def _invalidate_ip_dns_cache(rec: DNSRecord, ip: IPAddress | None) -> None:
    """Clear the IP's cached DNS-linkage fields when a record is deleted
    by the sync-reconcile stale-delete path.

    ``ip.fqdn`` and ``ip.forward_zone_id`` / ``ip.reverse_zone_id``
    behave as a cache of "what this IP is currently published as in
    DNS." They're set at publish time by :func:`_sync_dns_record`. When
    Sync DNS removes a stale record we also own the cache on the other
    side — if we don't clear it, the UI keeps showing the old FQDN
    (with the old domain suffix) even after the subnet's zone
    assignment has been removed.

    Only clears the side that matches the deleted record (forward vs
    reverse), so deleting a stale PTR doesn't wipe the forward FQDN if
    it still resolves.
    """
    if ip is None:
        return
    if rec.record_type == "PTR":
        if ip.reverse_zone_id == rec.zone_id:
            ip.reverse_zone_id = None
    else:  # A / AAAA / CNAME
        if ip.forward_zone_id == rec.zone_id or ip.dns_record_id == rec.id:
            ip.fqdn = None
            ip.forward_zone_id = None
            ip.dns_record_id = None


async def _sync_dns_record(
    db: AsyncSession,
    ip: IPAddress,
    subnet: Subnet,
    zone_id: uuid.UUID | None = None,
    action: str = "create",  # create | update | delete
) -> None:
    """Create, update, or delete the auto-generated A + PTR records for this IP.

    Forward A goes in the subnet's DNS zone (or explicitly passed zone_id);
    reverse PTR goes in the matching `kind=reverse` zone. Both records are
    pushed to the agent via RFC 2136 dynamic update through the record_op queue.
    """
    if action == "delete":
        result = await db.execute(
            select(DNSRecord)
            .where(
                DNSRecord.ip_address_id == ip.id,
                DNSRecord.auto_generated.is_(True),
            )
            .options(selectinload(DNSRecord.zone))
        )
        for record in result.scalars().all():
            zone = record.zone
            if zone is not None:
                await _enqueue_dns_op(
                    db,
                    zone,
                    "delete",
                    record.name,
                    record.record_type,
                    record.value,
                    record.ttl,
                )
            await db.delete(record)
        ip.dns_record_id = None
        # Preserve ``ip.fqdn`` and ``forward_zone_id`` / ``reverse_zone_id`` so
        # the orphan row keeps showing what was published before the delete
        # (greyed out in the UI), and so a later restore knows which zones to
        # put the records back into.
        return

    effective_zone_id = zone_id or await _resolve_effective_zone(db, subnet)
    if not effective_zone_id or not ip.hostname:
        return

    zone = await db.get(DNSZone, effective_zone_id)
    if not zone:
        return

    # Backfill the reverse zone if missing. Subnets created before DNS was
    # assigned won't have had `ensure_reverse_zone_for_subnet` run at create
    # time, so every IP allocation is an opportunity to catch up.
    try:
        from app.services.dns.reverse_zone import ensure_reverse_zone_for_subnet

        await ensure_reverse_zone_for_subnet(db, subnet, None)
    except Exception:  # noqa: BLE001 — best-effort, don't block IP allocation
        pass

    zone_domain = zone.name.rstrip(".")
    fqdn = f"{ip.hostname}.{zone_domain}"
    ip.fqdn = fqdn

    # Forward record type depends on the address family: AAAA for IPv6, A for IPv4.
    try:
        addr_obj = ipaddress.ip_address(str(ip.address))
    except ValueError:
        addr_obj = None
    forward_rtype = "AAAA" if isinstance(addr_obj, ipaddress.IPv6Address) else "A"

    # ── Forward A/AAAA ──────────────────────────────────────────────────────
    # Skip forward DNS for the default gateway placeholder hostname.
    # Every subnet has one, so syncing them all would create N copies of
    # `gateway.example.com` that resolve to different IPs — useless and noisy.
    # When a user renames the gateway IP to something specific (e.g.
    # "core-rtr1"), normal A-record sync resumes. Reverse PTR is still
    # created below since reverse lookups for the gateway IP are useful.
    is_default_gateway_name = ip.hostname == "gateway"

    # Fetch any pre-existing auto-generated A **or** AAAA for this IP — if the
    # address family changed we want to catch the stale record.
    result = await db.execute(
        select(DNSRecord).where(
            DNSRecord.ip_address_id == ip.id,
            DNSRecord.auto_generated.is_(True),
            DNSRecord.record_type.in_(["A", "AAAA"]),
        )
    )
    existing_a = result.scalars().all()

    if is_default_gateway_name:
        # Tear down any A/AAAA record that may have been published before the
        # user renamed the IP back to the default. PTR continues below.
        for record in existing_a:
            old_zone = await db.get(DNSZone, record.zone_id)
            if old_zone is not None:
                await _enqueue_dns_op(
                    db,
                    old_zone,
                    "delete",
                    record.name,
                    record.record_type,
                    record.value,
                    record.ttl,
                )
            await db.delete(record)
        ip.dns_record_id = None
    elif not existing_a:
        a_rec = DNSRecord(
            zone_id=effective_zone_id,
            name=ip.hostname,
            fqdn=fqdn,
            record_type=forward_rtype,
            value=str(ip.address),
            auto_generated=True,
            ip_address_id=ip.id,
            created_by_user_id=ip.created_by_user_id,
        )
        db.add(a_rec)
        await db.flush()
        ip.dns_record_id = a_rec.id
        ip.forward_zone_id = effective_zone_id
        await _enqueue_dns_op(db, zone, "create", ip.hostname, forward_rtype, str(ip.address), None)
    else:
        for record in existing_a:
            # If the zone changed OR the record_type changed (v4↔v6 swap),
            # rewrite: delete the stale record and create a fresh one.
            needs_rewrite = (
                record.zone_id != effective_zone_id or record.record_type != forward_rtype
            )
            if needs_rewrite:
                old_zone = await db.get(DNSZone, record.zone_id)
                if old_zone is not None:
                    await _enqueue_dns_op(
                        db,
                        old_zone,
                        "delete",
                        record.name,
                        record.record_type,
                        record.value,
                        record.ttl,
                    )
                await db.delete(record)
                new_a = DNSRecord(
                    zone_id=effective_zone_id,
                    name=ip.hostname,
                    fqdn=fqdn,
                    record_type=forward_rtype,
                    value=str(ip.address),
                    auto_generated=True,
                    ip_address_id=ip.id,
                    created_by_user_id=ip.created_by_user_id,
                )
                db.add(new_a)
                await db.flush()
                ip.dns_record_id = new_a.id
                ip.forward_zone_id = effective_zone_id
                await _enqueue_dns_op(
                    db, zone, "create", ip.hostname, forward_rtype, str(ip.address), None
                )
            else:
                changed = record.name != ip.hostname or record.value != str(ip.address)
                record.name = ip.hostname
                record.fqdn = fqdn
                record.value = str(ip.address)
                if changed:
                    await _enqueue_dns_op(
                        db,
                        zone,
                        "update",
                        ip.hostname,
                        forward_rtype,
                        str(ip.address),
                        record.ttl,
                    )

    # ── Reverse PTR ─────────────────────────────────────────────────────────
    try:
        ip_obj = ipaddress.ip_address(str(ip.address))
    except ValueError:
        return
    rev_zone = await _resolve_reverse_zone(db, subnet, ip_obj)
    if rev_zone is None:
        return  # No reverse zone covers this IP — quietly skip

    rev_pointer_full = ip_obj.reverse_pointer + "."
    rev_zone_name = rev_zone.name.rstrip(".") + "."
    # PTR record name is the leading labels stripped of the zone suffix
    if rev_pointer_full == rev_zone_name:
        ptr_name = "@"
    else:
        ptr_name = rev_pointer_full[: -(len(rev_zone_name) + 1)]
    ptr_value = fqdn + "."

    result = await db.execute(
        select(DNSRecord).where(
            DNSRecord.ip_address_id == ip.id,
            DNSRecord.auto_generated.is_(True),
            DNSRecord.record_type == "PTR",
        )
    )
    existing_ptr = result.scalars().all()

    if not existing_ptr:
        ptr_rec = DNSRecord(
            zone_id=rev_zone.id,
            name=ptr_name,
            fqdn=rev_pointer_full,
            record_type="PTR",
            value=ptr_value,
            auto_generated=True,
            ip_address_id=ip.id,
            created_by_user_id=ip.created_by_user_id,
        )
        db.add(ptr_rec)
        ip.reverse_zone_id = rev_zone.id
        await _enqueue_dns_op(db, rev_zone, "create", ptr_name, "PTR", ptr_value, None)
    else:
        for record in existing_ptr:
            if record.zone_id != rev_zone.id:
                old_zone = await db.get(DNSZone, record.zone_id)
                if old_zone is not None:
                    await _enqueue_dns_op(
                        db, old_zone, "delete", record.name, "PTR", record.value, record.ttl
                    )
                await db.delete(record)
                new_ptr = DNSRecord(
                    zone_id=rev_zone.id,
                    name=ptr_name,
                    fqdn=rev_pointer_full,
                    record_type="PTR",
                    value=ptr_value,
                    auto_generated=True,
                    ip_address_id=ip.id,
                    created_by_user_id=ip.created_by_user_id,
                )
                db.add(new_ptr)
                ip.reverse_zone_id = rev_zone.id
                await _enqueue_dns_op(db, rev_zone, "create", ptr_name, "PTR", ptr_value, None)
            else:
                changed = record.value != ptr_value or record.name != ptr_name
                record.name = ptr_name
                record.fqdn = rev_pointer_full
                record.value = ptr_value
                if changed:
                    await _enqueue_dns_op(
                        db, rev_zone, "update", ptr_name, "PTR", ptr_value, record.ttl
                    )


def _compute_free_cidrs(
    block_network: str,
    child_networks: list[str],
    max_results: int = 200,
) -> list[dict[str, Any]]:
    """Return a sorted list of free CIDR ranges inside ``block_network``.

    Each entry has ``network`` (CIDR), ``first``, ``last`` (string IPs),
    and ``size`` (usable address count for the free gap, counted as raw
    addresses — network/broadcast are not excluded because a free gap
    is not a routable subnet yet).

    The algorithm subtracts each existing child network from the block
    using ``ipaddress.Network.address_exclude`` repeatedly. This is
    correct for arbitrary combinations of child sizes and does not
    require them to be aligned.
    """
    block = ipaddress.ip_network(block_network, strict=False)

    # Start with a single working set containing the block itself.
    working: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [block]

    # Sort children by prefixlen desc so smaller ones excluded first is fine —
    # address_exclude handles either ordering. We just iterate.
    for raw in child_networks:
        child = ipaddress.ip_network(raw, strict=False)
        next_working: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for net in working:
            if child == net:
                # fully consumed
                continue
            if child.subnet_of(net):  # type: ignore[arg-type]
                next_working.extend(net.address_exclude(child))  # type: ignore[arg-type]
            elif net.subnet_of(child):  # type: ignore[arg-type]
                # net is fully covered by child → drop
                continue
            else:
                next_working.append(net)
        working = next_working

    # Sort by network address and build result
    working.sort(key=lambda n: int(n.network_address))
    out: list[dict[str, Any]] = []
    for net in working[:max_results]:
        out.append(
            {
                "network": str(net),
                "first": str(net.network_address),
                "last": str(net.broadcast_address),
                "size": net.num_addresses,
                "prefix_len": net.prefixlen,
            }
        )
    return out


def _audit(
    user: Any,
    action: str,
    resource_type: str,
    resource_id: str,
    resource_display: str,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> AuditLog:
    return AuditLog(
        user_id=user.id,
        user_display_name=user.display_name,
        auth_source=user.auth_source,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_display=resource_display,
        old_value=old_value,
        new_value=new_value,
        result="success",
    )


# ── Schemas ────────────────────────────────────────────────────────────────────


class IPSpaceCreate(BaseModel):
    name: str
    description: str = ""
    is_default: bool = False
    tags: dict[str, Any] = {}
    dns_group_ids: list[str] = []
    dns_zone_id: str | None = None
    dns_additional_zone_ids: list[str] = []
    dhcp_server_group_id: uuid.UUID | None = None


class IPSpaceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_default: bool | None = None
    tags: dict[str, Any] | None = None
    dns_group_ids: list[str] | None = None
    dns_zone_id: str | None = None
    dns_additional_zone_ids: list[str] | None = None
    dhcp_server_group_id: uuid.UUID | None = None


class IPSpaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    is_default: bool
    tags: dict[str, Any]
    dns_group_ids: list[str] = []
    dns_zone_id: str | None = None
    dns_additional_zone_ids: list[str] = []
    dhcp_server_group_id: uuid.UUID | None = None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("dns_group_ids", "dns_additional_zone_ids", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> list[str]:
        return v if isinstance(v, list) else []


class IPBlockCreate(BaseModel):
    space_id: uuid.UUID
    parent_block_id: uuid.UUID | None = None
    network: str
    name: str = ""
    description: str = ""
    tags: dict[str, Any] = {}
    custom_fields: dict[str, Any] = {}
    dns_group_ids: list[str] = []
    dns_zone_id: str | None = None
    dns_additional_zone_ids: list[str] = []
    dns_inherit_settings: bool = True
    dhcp_server_group_id: uuid.UUID | None = None
    dhcp_inherit_settings: bool = True

    @field_validator("network")
    @classmethod
    def validate_network(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError:
            raise ValueError(f"Invalid CIDR notation: {v}")
        return v


class IPBlockUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    parent_block_id: uuid.UUID | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None
    dns_group_ids: list[str] | None = None
    dns_zone_id: str | None = None
    dns_additional_zone_ids: list[str] | None = None
    dns_inherit_settings: bool | None = None
    dhcp_server_group_id: uuid.UUID | None = None
    dhcp_inherit_settings: bool | None = None


class IPBlockResponse(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    parent_block_id: uuid.UUID | None
    network: str
    name: str
    description: str
    utilization_percent: float
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    dns_group_ids: list[str] | None
    dns_zone_id: str | None
    dns_additional_zone_ids: list[str] | None
    dns_inherit_settings: bool
    dhcp_server_group_id: uuid.UUID | None = None
    dhcp_inherit_settings: bool = True
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("network", mode="before")
    @classmethod
    def coerce_network(cls, v: Any) -> str:
        return str(v)


class SubnetCreate(BaseModel):
    space_id: uuid.UUID
    block_id: uuid.UUID
    network: str
    name: str = ""
    description: str = ""
    vlan_id: int | None = None
    vxlan_id: int | None = None
    vlan_ref_id: uuid.UUID | None = None
    gateway: str | None = None  # None → auto-assign first usable IP
    status: str = "active"
    skip_auto_addresses: bool = (
        False  # True for loopbacks/P2P — skips network/broadcast/gateway records
    )
    # Reverse-zone auto-create controls (see services/dns/reverse_zone.py).
    # The matching reverse zone is created automatically when dns_group_id or
    # dns_zone_id is supplied (or inherited via a future IPAM column); opt out
    # with skip_reverse_zone=True.
    dns_group_id: uuid.UUID | None = None
    dns_zone_id: uuid.UUID | None = None
    skip_reverse_zone: bool = False
    dns_servers: list[str] | None = None
    domain_name: str | None = None
    tags: dict[str, Any] = {}
    custom_fields: dict[str, Any] = {}
    dns_group_ids: list[str] = []
    dns_zone_id: str | None = None
    dns_additional_zone_ids: list[str] = []
    dns_inherit_settings: bool = True
    dhcp_server_group_id: uuid.UUID | None = None
    dhcp_inherit_settings: bool = True
    # DDNS — see Subnet model. Defaults mirror the DB: off by default,
    # policy ``client_or_generated`` only takes effect when enabled.
    ddns_enabled: bool = False
    ddns_hostname_policy: str = "client_or_generated"
    ddns_domain_override: str | None = None
    ddns_ttl: int | None = None

    @field_validator("ddns_hostname_policy")
    @classmethod
    def validate_ddns_policy_create(cls, v: str) -> str:
        allowed = {"client_provided", "client_or_generated", "always_generate", "disabled"}
        if v not in allowed:
            raise ValueError(f"ddns_hostname_policy must be one of: {', '.join(sorted(allowed))}")
        return v

    @field_validator("network")
    @classmethod
    def validate_network(cls, v: str) -> str:
        try:
            ipaddress.ip_network(v, strict=True)
            return v
        except ValueError:
            pass
        # If strict fails, check whether host bits are the problem
        try:
            canonical = str(ipaddress.ip_network(v, strict=False))
            raise ValueError(f"Host bits are set in '{v}'. Did you mean {canonical}?")
        except ValueError as e:
            if "Did you mean" in str(e):
                raise
            raise ValueError(f"Invalid CIDR notation: {v}")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"active", "deprecated", "reserved", "quarantine"}
        if v not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        return v


class SubnetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    block_id: uuid.UUID | None = None
    vlan_id: int | None = None
    vxlan_id: int | None = None
    vlan_ref_id: uuid.UUID | None = None
    gateway: str | None = None
    status: str | None = None
    dns_servers: list[str] | None = None
    domain_name: str | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None
    # When True: remove network/broadcast/gateway auto records.
    # When False: create them if not already present.
    manage_auto_addresses: bool | None = None
    dns_group_ids: list[str] | None = None
    dns_zone_id: str | None = None
    dns_additional_zone_ids: list[str] | None = None
    dns_inherit_settings: bool | None = None
    dhcp_server_group_id: uuid.UUID | None = None
    dhcp_inherit_settings: bool | None = None
    ddns_enabled: bool | None = None
    ddns_hostname_policy: str | None = None
    ddns_domain_override: str | None = None
    ddns_ttl: int | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"active", "deprecated", "reserved", "quarantine"}
        if v not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        return v

    @field_validator("ddns_hostname_policy")
    @classmethod
    def validate_ddns_policy_update(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"client_provided", "client_or_generated", "always_generate", "disabled"}
        if v not in allowed:
            raise ValueError(f"ddns_hostname_policy must be one of: {', '.join(sorted(allowed))}")
        return v


class SubnetVLANRef(BaseModel):
    id: uuid.UUID
    router_id: uuid.UUID
    router_name: str | None = None
    vlan_id: int
    name: str

    model_config = {"from_attributes": True}


class SubnetResponse(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    # Nullable because the DB column is ``NULL``-able even though the ORM
    # model declares it non-null — historical schema drift. A subnet with
    # ``block_id IS NULL`` is an orphan from before blocks were mandatory;
    # surface it rather than 500 the whole list endpoint.
    block_id: uuid.UUID | None
    network: str
    name: str
    description: str
    vlan_id: int | None
    vxlan_id: int | None
    vlan_ref_id: uuid.UUID | None = None
    vlan: SubnetVLANRef | None = None
    gateway: str | None
    status: str
    utilization_percent: float
    total_ips: int
    allocated_ips: int
    dns_servers: list[str] | None
    domain_name: str | None
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    dns_group_ids: list[str] | None
    dns_zone_id: str | None
    dns_additional_zone_ids: list[str] | None
    dns_inherit_settings: bool
    dhcp_server_group_id: uuid.UUID | None = None
    dhcp_inherit_settings: bool = True
    ddns_enabled: bool = False
    ddns_hostname_policy: str = "client_or_generated"
    ddns_domain_override: str | None = None
    ddns_ttl: int | None = None
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("network", "gateway", mode="before")
    @classmethod
    def coerce_inet(cls, v: Any) -> Any:
        return str(v) if v is not None else v

    @model_validator(mode="before")
    @classmethod
    def _attach_vlan(cls, data: Any) -> Any:
        # When serializing a Subnet ORM instance, enrich with nested `vlan` from `vlan_ref`.
        if isinstance(data, Subnet):
            vref = getattr(data, "vlan_ref", None)
            if vref is not None:
                return {
                    **{c.name: getattr(data, c.name) for c in data.__table__.columns},
                    "vlan": {
                        "id": vref.id,
                        "router_id": vref.router_id,
                        "router_name": getattr(getattr(vref, "router", None), "name", None),
                        "vlan_id": vref.vlan_id,
                        "name": vref.name,
                    },
                }
        return data


class EffectiveDnsResponse(BaseModel):
    dns_group_ids: list[str]
    dns_zone_id: str | None
    dns_additional_zone_ids: list[str]
    inherited_from_block_id: str | None


class EffectiveDhcpResponse(BaseModel):
    """Effective DHCP server-group resolution for a space/block/subnet.

    Walks the hierarchy until it finds a level that explicitly sets a server
    group (or opts out of inheritance). ``inherited_from_block_id`` is set
    when the value came from an ancestor block; None when it came from the
    level itself or the space.
    """

    dhcp_server_group_id: str | None
    inherited_from_block_id: str | None
    inherited_from_space: bool = False


class AliasInput(BaseModel):
    name: str  # label within the zone (e.g. "www", "mail")
    record_type: str = "CNAME"  # CNAME → points to the IP's FQDN; A → points to the IP

    @field_validator("record_type")
    @classmethod
    def _rt(cls, v: str) -> str:
        v = v.upper()
        if v not in {"CNAME", "A"}:
            raise ValueError("alias record_type must be CNAME or A")
        return v

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        v = v.strip().rstrip(".")
        if not v:
            raise ValueError("alias name is required")
        return v


class IPAddressCreate(BaseModel):
    address: str
    status: str = "allocated"
    hostname: str
    mac_address: str | None = None
    description: str = ""
    owner_user_id: uuid.UUID | None = None
    custom_fields: dict[str, Any] = {}
    tags: dict[str, Any] = {}
    dns_zone_id: str | None = None  # explicit zone override; falls back to subnet's effective DNS
    aliases: list[AliasInput] = []
    # When False (default), the server returns 409 if the pending assignment
    # collides with another IP's FQDN or MAC. Clients re-submit with True
    # after the user confirms the warning.
    force: bool = False

    @field_validator("hostname")
    @classmethod
    def hostname_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Hostname is required")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"allocated", "reserved", "dhcp", "static_dhcp", "deprecated"}
        if v not in allowed:
            raise ValueError(
                f"status must be one of: {', '.join(sorted(allowed))}. "
                "Use 'reserved' for gateway/infrastructure IPs."
            )
        return v


class IPAddressUpdate(BaseModel):
    status: str | None = None
    hostname: str | None = None
    mac_address: str | None = None
    description: str | None = None
    owner_user_id: uuid.UUID | None = None
    custom_fields: dict[str, Any] | None = None
    tags: dict[str, Any] | None = None
    dns_zone_id: str | None = None  # explicit zone override for DNS record
    # See IPAddressCreate.force.
    force: bool = False

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"available", "allocated", "reserved", "static_dhcp", "deprecated"}
        if v not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        return v


class IPAddressResponse(BaseModel):
    id: uuid.UUID
    subnet_id: uuid.UUID
    address: str
    status: str
    hostname: str | None
    fqdn: str | None
    mac_address: str | None
    description: str
    owner_user_id: uuid.UUID | None
    last_seen_at: datetime | None
    last_seen_method: str | None
    custom_fields: dict[str, Any]
    tags: dict[str, Any]
    # Linkage (§3) — populated by Wave 3 DDNS/DHCP integration.
    forward_zone_id: uuid.UUID | None = None
    reverse_zone_id: uuid.UUID | None = None
    dns_record_id: uuid.UUID | None = None
    dhcp_lease_id: str | None = None
    static_assignment_id: str | None = None
    # True when this IPAM row was auto-created by the DHCP lease-pull task
    # mirroring a dynamic lease. Surfaced so the UI can suppress the per-IP
    # edit/delete actions — the row reflects server state, not user intent,
    # and any edit would be overwritten on the next pull cycle.
    auto_from_lease: bool = False
    # Number of user-added CNAME/A alias records on this IP (excludes the primary A).
    # Populated in list/get endpoints via a bulk lookup; defaults to 0 on other paths.
    alias_count: int = 0
    created_at: datetime
    modified_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("address", "mac_address", mode="before")
    @classmethod
    def coerce_inet(cls, v: Any) -> Any:
        return str(v) if v is not None else v


class NextIPRequest(BaseModel):
    strategy: str = "sequential"
    status: str = "allocated"
    hostname: str
    mac_address: str | None = None
    description: str = ""
    custom_fields: dict[str, Any] = {}
    tags: dict[str, Any] = {}
    dns_zone_id: str | None = None  # explicit zone override; falls back to subnet's effective DNS
    aliases: list[AliasInput] = []
    # See IPAddressCreate.force.
    force: bool = False

    @field_validator("hostname")
    @classmethod
    def hostname_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Hostname is required")
        return v

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        if v not in {"sequential", "random"}:
            raise ValueError("strategy must be 'sequential' or 'random'")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"allocated", "reserved", "dhcp", "static_dhcp"}
        if v not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
        return v


# ── IP Spaces ──────────────────────────────────────────────────────────────────


@router.get("/spaces", response_model=list[IPSpaceResponse])
async def list_spaces(current_user: CurrentUser, db: DB) -> list[IPSpace]:
    result = await db.execute(select(IPSpace).order_by(IPSpace.name))
    return list(result.scalars().all())


@router.post("/spaces", response_model=IPSpaceResponse, status_code=status.HTTP_201_CREATED)
async def create_space(body: IPSpaceCreate, current_user: CurrentUser, db: DB) -> IPSpace:
    # Pre-check the unique-name constraint so we can return a clean 409
    # instead of letting the DB raise an IntegrityError (→ 500).
    existing = await db.execute(select(IPSpace).where(IPSpace.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An IP space named {body.name!r} already exists",
        )
    space = IPSpace(**body.model_dump())
    db.add(space)
    await db.flush()
    db.add(
        _audit(
            current_user,
            "create",
            "ip_space",
            str(space.id),
            body.name,
            new_value=body.model_dump(),
        )
    )
    await db.commit()
    await db.refresh(space)
    logger.info("ip_space_created", space_id=str(space.id), name=space.name)
    return space


@router.get("/spaces/{space_id}", response_model=IPSpaceResponse)
async def get_space(space_id: uuid.UUID, current_user: CurrentUser, db: DB) -> IPSpace:
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")
    return space


@router.put("/spaces/{space_id}", response_model=IPSpaceResponse)
async def update_space(
    space_id: uuid.UUID, body: IPSpaceUpdate, current_user: CurrentUser, db: DB
) -> IPSpace:
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    old = {"name": space.name, "description": space.description, "tags": space.tags}
    changes = body.model_dump(exclude_none=True, exclude={"dhcp_server_group_id"})
    for field, value in changes.items():
        setattr(space, field, value)
    # Handle DHCP fields explicitly so explicit null (clear) is preserved.
    if "dhcp_server_group_id" in body.model_fields_set:
        space.dhcp_server_group_id = body.dhcp_server_group_id
        changes["dhcp_server_group_id"] = (
            str(body.dhcp_server_group_id) if body.dhcp_server_group_id else None
        )

    db.add(
        _audit(
            current_user,
            "update",
            "ip_space",
            str(space.id),
            space.name,
            old_value=old,
            new_value=changes,
        )
    )
    await db.commit()
    await db.refresh(space)
    return space


@router.get("/spaces/{space_id}/effective-dns", response_model=EffectiveDnsResponse)
async def get_effective_space_dns(
    space_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> EffectiveDnsResponse:
    """Return DNS settings set directly on the space (used as the top-level default)."""
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")
    return EffectiveDnsResponse(
        dns_group_ids=space.dns_group_ids or [],
        dns_zone_id=space.dns_zone_id,
        dns_additional_zone_ids=space.dns_additional_zone_ids or [],
        inherited_from_block_id=None,
    )


@router.get("/spaces/{space_id}/effective-dhcp", response_model=EffectiveDhcpResponse)
async def get_effective_space_dhcp(
    space_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> EffectiveDhcpResponse:
    """Return the DHCP server group set directly on the space."""
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")
    return EffectiveDhcpResponse(
        dhcp_server_group_id=(
            str(space.dhcp_server_group_id) if space.dhcp_server_group_id else None
        ),
        inherited_from_block_id=None,
        inherited_from_space=False,
    )


@router.delete("/spaces/{space_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_space(space_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    # Subnet.space_id is ondelete=RESTRICT so a naive delete bubbles as 500
    # when anything is still anchored here. Pre-check blocks and subnets so
    # the UI gets a clear 409 with a count instead of an opaque server error.
    subnet_count = (
        await db.execute(
            select(func.count()).select_from(Subnet).where(Subnet.space_id == space_id)
        )
    ).scalar_one()
    block_count = (
        await db.execute(
            select(func.count()).select_from(IPBlock).where(IPBlock.space_id == space_id)
        )
    ).scalar_one()
    if subnet_count or block_count:
        parts = []
        if block_count:
            parts.append(f"{block_count} block(s)")
        if subnet_count:
            parts.append(f"{subnet_count} subnet(s)")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"IP space {space.name!r} still contains {' and '.join(parts)}. "
                "Delete or move them before deleting the space."
            ),
        )

    db.add(
        _audit(
            current_user,
            "delete",
            "ip_space",
            str(space.id),
            space.name,
            old_value={"name": space.name},
        )
    )
    await db.delete(space)
    await db.commit()


# ── IP Blocks ──────────────────────────────────────────────────────────────────


@router.get("/blocks", response_model=list[IPBlockResponse])
async def list_blocks(
    current_user: CurrentUser,
    db: DB,
    space_id: uuid.UUID | None = None,
) -> list[IPBlock]:
    query = select(IPBlock).order_by(IPBlock.network)
    if space_id:
        query = query.where(IPBlock.space_id == space_id)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/blocks", response_model=IPBlockResponse, status_code=status.HTTP_201_CREATED)
async def create_block(body: IPBlockCreate, current_user: CurrentUser, db: DB) -> IPBlock:
    # Verify space exists
    if await db.get(IPSpace, body.space_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    # Verify parent block exists and belongs to the same space
    if body.parent_block_id:
        parent = await db.get(IPBlock, body.parent_block_id)
        if parent is None or parent.space_id != body.space_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Parent block not found in this space"
            )
        # Validate child fits within parent
        child_net = _parse_network(body.network)
        parent_net = _parse_network(str(parent.network))
        if not child_net.subnet_of(parent_net):  # type: ignore[arg-type]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{body.network} is not contained within parent block {parent.network}",
            )

    # Reject duplicates / overlaps at the same tree level.
    canonical = str(_parse_network(body.network))
    await _assert_no_block_overlap(db, body.space_id, canonical, body.parent_block_id)

    block = IPBlock(**body.model_dump())
    db.add(block)
    await db.flush()
    db.add(
        _audit(
            current_user,
            "create",
            "ip_block",
            str(block.id),
            f"{body.network} ({body.name})",
            new_value=body.model_dump(mode="json"),
        )
    )
    await db.commit()
    await db.refresh(block)
    logger.info("ip_block_created", block_id=str(block.id), network=block.network)
    return block


@router.get("/blocks/{block_id}", response_model=IPBlockResponse)
async def get_block(block_id: uuid.UUID, current_user: CurrentUser, db: DB) -> IPBlock:
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP block not found")
    return block


@router.put("/blocks/{block_id}", response_model=IPBlockResponse)
async def update_block(
    block_id: uuid.UUID, body: IPBlockUpdate, current_user: CurrentUser, db: DB
) -> IPBlock:
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP block not found")

    old = {
        "name": block.name,
        "description": block.description,
        "parent_block_id": str(block.parent_block_id) if block.parent_block_id else None,
    }

    # Handle parent_block_id (reparent) separately so we can run validation and
    # rollups. `parent_block_id` appearing in `model_fields_set` means the caller
    # set it explicitly (including to null for "move to top level").
    old_parent_id = block.parent_block_id
    reparent_requested = "parent_block_id" in body.model_fields_set
    if reparent_requested:
        new_parent_id = body.parent_block_id
        if new_parent_id is not None:
            if new_parent_id == block.id:
                raise HTTPException(status_code=422, detail="A block cannot be its own parent")
            new_parent = await db.get(IPBlock, new_parent_id)
            if new_parent is None or new_parent.space_id != block.space_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Target parent block not found in this space",
                )
            # CIDR containment
            child_net = _parse_network(str(block.network))
            parent_net = _parse_network(str(new_parent.network))
            if not child_net.subnet_of(parent_net):  # type: ignore[arg-type]
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{block.network} is not contained within target parent {new_parent.network}",
                )
            # Cycle detection: walk new_parent's ancestry and ensure we don't find block.id
            cursor: IPBlock | None = new_parent
            while cursor is not None:
                if cursor.id == block.id:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Reparenting would create a cycle",
                    )
                if cursor.parent_block_id is None:
                    break
                cursor = await db.get(IPBlock, cursor.parent_block_id)
        # Reject overlap with future siblings under the new parent (or with
        # top-level blocks when moving to the root).
        await _assert_no_block_overlap(
            db,
            block.space_id,
            str(block.network),
            new_parent_id,
            exclude_id=block.id,
        )
        block.parent_block_id = new_parent_id

    changes = body.model_dump(
        exclude_none=True,
        exclude={
            "dns_group_ids",
            "dns_zone_id",
            "dns_additional_zone_ids",
            "dns_inherit_settings",
            "dhcp_server_group_id",
            "dhcp_inherit_settings",
            "parent_block_id",
        },
    )
    for field, value in changes.items():
        setattr(block, field, value)
    # Handle DNS fields explicitly so boolean False and explicit null are preserved
    dns_fields = {"dns_group_ids", "dns_zone_id", "dns_additional_zone_ids", "dns_inherit_settings"}
    for field in dns_fields & body.model_fields_set:
        setattr(block, field, getattr(body, field))
        changes[field] = getattr(body, field)
    # Same treatment for the DHCP fields.
    dhcp_fields = {"dhcp_server_group_id", "dhcp_inherit_settings"}
    for field in dhcp_fields & body.model_fields_set:
        val = getattr(body, field)
        setattr(block, field, val)
        changes[field] = str(val) if isinstance(val, uuid.UUID) else val
    if reparent_requested:
        changes["parent_block_id"] = str(body.parent_block_id) if body.parent_block_id else None

    db.add(
        _audit(
            current_user,
            "update",
            "ip_block",
            str(block.id),
            f"{block.network} ({block.name})",
            old_value=old,
            new_value=changes,
        )
    )
    await db.flush()

    # Update utilization rollups for old and new ancestor chains
    if reparent_requested and old_parent_id != block.parent_block_id:
        if old_parent_id:
            await _update_block_utilization(db, old_parent_id)
        if block.parent_block_id:
            await _update_block_utilization(db, block.parent_block_id)

    await db.commit()
    await db.refresh(block)
    return block


@router.delete("/blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_block(block_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP block not found")

    # Refuse if anything is anchored below this block. Child blocks cascade,
    # which would silently nuke a chunk of the tree. Subnet.block_id is
    # RESTRICT at the DB level so a subnet-having block would bubble a 500
    # anyway — but the DB column is also historically nullable (schema
    # drift), so a naive delete can end up orphaning subnets with
    # ``block_id=NULL``. Check explicitly and return a useful 409.
    subnet_count = (
        await db.execute(
            select(func.count()).select_from(Subnet).where(Subnet.block_id == block_id)
        )
    ).scalar_one()
    child_block_count = (
        await db.execute(
            select(func.count()).select_from(IPBlock).where(IPBlock.parent_block_id == block_id)
        )
    ).scalar_one()
    if subnet_count or child_block_count:
        parts = []
        if child_block_count:
            parts.append(f"{child_block_count} child block(s)")
        if subnet_count:
            parts.append(f"{subnet_count} subnet(s)")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Block {block.network} still contains {' and '.join(parts)}. "
                "Delete or move them before deleting the block."
            ),
        )

    db.add(
        _audit(
            current_user,
            "delete",
            "ip_block",
            str(block.id),
            f"{block.network} ({block.name})",
            old_value={"network": str(block.network)},
        )
    )
    await db.delete(block)
    await db.commit()


@router.get("/blocks/{block_id}/available-subnets", response_model=list[str])
async def get_available_subnets(
    block_id: uuid.UUID,
    prefix_len: int = Query(
        ...,
        ge=1,
        le=128,
        description="Desired prefix length — /1-/32 for IPv4 blocks, /1-/128 for IPv6",
    ),
    limit: int = Query(20, ge=1, le=50),
    current_user: CurrentUser = ...,  # type: ignore[assignment]
    db: DB = ...,  # type: ignore[assignment]
) -> list[str]:
    """Return available /prefix_len subnets within this block, sorted sequentially.

    Accepts both IPv4 (/1-/32) and IPv6 (/1-/128) prefix lengths — the block's
    own family is inferred from its CIDR.
    """
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")

    block_net = ipaddress.ip_network(str(block.network), strict=False)
    max_prefix = 32 if isinstance(block_net, ipaddress.IPv4Network) else 128
    if prefix_len > max_prefix:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"prefix_len {prefix_len} exceeds max {max_prefix} for "
                f"{'IPv4' if max_prefix == 32 else 'IPv6'} block"
            ),
        )
    if prefix_len <= block_net.prefixlen:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"prefix_len {prefix_len} must be greater than block prefix length {block_net.prefixlen}",
        )

    result = await db.execute(
        text(
            "SELECT network FROM subnet "
            "WHERE space_id = CAST(:sid AS uuid) AND network && CAST(:net AS cidr)"
        ),
        {"sid": str(block.space_id), "net": str(block.network)},
    )
    existing = [ipaddress.ip_network(str(row[0]), strict=False) for row in result.fetchall()]

    available: list[str] = []
    scanned = 0
    max_scan = limit * 200  # cap iterations to avoid runaway loops on large sparse blocks
    for candidate in block_net.subnets(new_prefix=prefix_len):
        if len(available) >= limit or scanned >= max_scan:
            break
        scanned += 1
        if not any(candidate.overlaps(ex) for ex in existing):
            available.append(str(candidate))

    return available


class FreeCidrRange(BaseModel):
    network: str
    first: str
    last: str
    size: int
    prefix_len: int


@router.get("/blocks/{block_id}/free-space", response_model=list[FreeCidrRange])
async def get_block_free_space(
    block_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> list[FreeCidrRange]:
    """Return free CIDR ranges inside this block.

    Free space is computed by subtracting all direct-child blocks and direct-child
    subnets from the block's CIDR. (Nested blocks' own children are accounted for
    inside those nested blocks, not here.)
    """
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")

    child_blocks = await db.execute(
        select(IPBlock.network).where(IPBlock.parent_block_id == block.id)
    )
    child_subnets = await db.execute(select(Subnet.network).where(Subnet.block_id == block.id))
    occupied = [str(n) for (n,) in child_blocks.all()] + [str(n) for (n,) in child_subnets.all()]
    ranges = _compute_free_cidrs(str(block.network), occupied)
    return [FreeCidrRange(**r) for r in ranges]


@router.get("/blocks/{block_id}/effective-dns", response_model=EffectiveDnsResponse)
async def get_effective_block_dns(
    block_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> EffectiveDnsResponse:
    """Resolve effective DNS settings by walking up block ancestors then the space."""
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")

    current = block
    while current is not None:
        if not current.dns_inherit_settings:
            return EffectiveDnsResponse(
                dns_group_ids=current.dns_group_ids or [],
                dns_zone_id=current.dns_zone_id,
                dns_additional_zone_ids=current.dns_additional_zone_ids or [],
                inherited_from_block_id=str(current.id) if current.id != block_id else None,
            )
        if current.parent_block_id:
            current = await db.get(IPBlock, current.parent_block_id)
        else:
            # Reached the root block — fall through to the space-level settings
            space = await db.get(IPSpace, current.space_id)
            if space and (space.dns_group_ids or space.dns_zone_id):
                return EffectiveDnsResponse(
                    dns_group_ids=space.dns_group_ids or [],
                    dns_zone_id=space.dns_zone_id,
                    dns_additional_zone_ids=space.dns_additional_zone_ids or [],
                    inherited_from_block_id=None,
                )
            break

    return EffectiveDnsResponse(
        dns_group_ids=[], dns_zone_id=None, dns_additional_zone_ids=[], inherited_from_block_id=None
    )


@router.get("/blocks/{block_id}/effective-dhcp", response_model=EffectiveDhcpResponse)
async def get_effective_block_dhcp(
    block_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> EffectiveDhcpResponse:
    """Resolve effective DHCP server group by walking block ancestors then the space."""
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")

    current = block
    while current is not None:
        if not current.dhcp_inherit_settings:
            return EffectiveDhcpResponse(
                dhcp_server_group_id=(
                    str(current.dhcp_server_group_id) if current.dhcp_server_group_id else None
                ),
                inherited_from_block_id=str(current.id) if current.id != block_id else None,
                inherited_from_space=False,
            )
        if current.parent_block_id:
            current = await db.get(IPBlock, current.parent_block_id)
        else:
            # Root block → fall through to the space-level default.
            space = await db.get(IPSpace, current.space_id)
            if space and space.dhcp_server_group_id:
                return EffectiveDhcpResponse(
                    dhcp_server_group_id=str(space.dhcp_server_group_id),
                    inherited_from_block_id=None,
                    inherited_from_space=True,
                )
            break

    return EffectiveDhcpResponse(
        dhcp_server_group_id=None, inherited_from_block_id=None, inherited_from_space=False
    )


# ── Subnets ────────────────────────────────────────────────────────────────────


@router.get("/subnets", response_model=list[SubnetResponse])
async def list_subnets(
    current_user: CurrentUser,
    db: DB,
    space_id: uuid.UUID | None = None,
    block_id: uuid.UUID | None = None,
    vlan_ref_id: uuid.UUID | None = None,
) -> list[Subnet]:
    query = select(Subnet).order_by(Subnet.network)
    if space_id:
        query = query.where(Subnet.space_id == space_id)
    if block_id:
        query = query.where(Subnet.block_id == block_id)
    if vlan_ref_id:
        query = query.where(Subnet.vlan_ref_id == vlan_ref_id)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/subnets", response_model=SubnetResponse, status_code=status.HTTP_201_CREATED)
async def create_subnet(body: SubnetCreate, current_user: CurrentUser, db: DB) -> Subnet:
    if await db.get(IPSpace, body.space_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

    block = await db.get(IPBlock, body.block_id)
    if block is None or block.space_id != body.space_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Block not found in this space"
        )

    net = _parse_network(body.network)
    canonical = str(net)  # normalise e.g. "10.0.0.1/24" → "10.0.0.0/24"

    await _assert_no_overlap(db, body.space_id, canonical)

    # Validate gateway is within the subnet if explicitly provided
    if body.gateway:
        try:
            gw = ipaddress.ip_address(body.gateway)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid gateway IP: {body.gateway}")
        if gw not in net:
            raise HTTPException(
                status_code=422,
                detail=f"Gateway {body.gateway} is not within subnet {canonical}",
            )

    total = _total_ips(net)

    # Resolve vlan_ref_id → authoritative vlan_id tag
    if body.vlan_ref_id is not None:
        vlan_obj = await db.get(VLAN, body.vlan_ref_id)
        if vlan_obj is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Referenced VLAN not found"
            )
        if body.vlan_id is not None and body.vlan_id != vlan_obj.vlan_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"vlan_id ({body.vlan_id}) does not match the tag of the "
                    f"referenced VLAN ({vlan_obj.vlan_id})"
                ),
            )
        # Override to enforce consistency
        body.vlan_id = vlan_obj.vlan_id

    subnet = Subnet(
        **{
            **body.model_dump(
                exclude={
                    "skip_auto_addresses",
                    "skip_reverse_zone",
                    "dns_group_id",
                    "dns_zone_id",
                }
            ),
            "network": canonical,
        },
        total_ips=total,
        utilization_percent=0.0,
        allocated_ips=0,
    )
    db.add(subnet)
    await db.flush()

    # For standard subnets (prefixlen < 31), create network, broadcast, and gateway records
    # unless skip_auto_addresses is set (e.g. loopbacks, point-to-point links).
    # IPv6 has no broadcast; the network address itself is usable, but we
    # still create a "network" pseudo-row (same UX) plus the gateway row.
    auto_created: list[str] = []
    is_v6 = isinstance(net, ipaddress.IPv6Network)
    if net.prefixlen < 31 and not body.skip_auto_addresses:
        # Network address (e.g. 10.0.1.0 / 2001:db8::)
        db.add(
            IPAddress(
                subnet_id=subnet.id,
                address=str(net.network_address),
                status="network",
                description="Network address",
                created_by_user_id=current_user.id,
            )
        )
        auto_created.append(str(net.network_address))

        if not is_v6:
            # Broadcast address (IPv4 only — IPv6 has no broadcast)
            db.add(
                IPAddress(
                    subnet_id=subnet.id,
                    address=str(net.broadcast_address),
                    status="broadcast",
                    description="Broadcast address",
                    created_by_user_id=current_user.id,
                )
            )
            auto_created.append(str(net.broadcast_address))

        # Gateway — use provided or default to first usable host
        gw_addr = body.gateway or str(net.network_address + 1)
        db.add(
            IPAddress(
                subnet_id=subnet.id,
                address=gw_addr,
                status="reserved",
                description="Gateway",
                hostname="gateway",
                created_by_user_id=current_user.id,
            )
        )
        subnet.gateway = gw_addr
        auto_created.append(gw_addr)

    db.add(
        _audit(
            current_user,
            "create",
            "subnet",
            str(subnet.id),
            f"{canonical} ({body.name})",
            new_value={**body.model_dump(mode="json"), "network": canonical},
        )
    )
    await db.flush()

    if auto_created:
        await _update_utilization(db, subnet.id)

    await _update_block_utilization(db, subnet.block_id)

    # Auto-create the matching reverse zone if a DNS assignment was supplied
    # (or will be inherited, once the IPAM model carries dns_group_ids).
    if not body.skip_reverse_zone and (
        body.dns_group_id
        or body.dns_zone_id
        or getattr(subnet, "dns_zone_id", None)
        or getattr(subnet, "dns_group_ids", None)
    ):
        from app.services.dns.reverse_zone import ensure_reverse_zone_for_subnet

        await ensure_reverse_zone_for_subnet(
            db,
            subnet,
            current_user,
            dns_group_id=body.dns_group_id,
            dns_zone_id=body.dns_zone_id,
        )

    await db.commit()
    await db.refresh(subnet)
    logger.info(
        "subnet_created", subnet_id=str(subnet.id), network=canonical, gateway=subnet.gateway
    )
    return subnet


@router.get("/subnets/{subnet_id}", response_model=SubnetResponse)
async def get_subnet(subnet_id: uuid.UUID, current_user: CurrentUser, db: DB) -> Subnet:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")
    return subnet


@router.get("/subnets/{subnet_id}/effective-dns", response_model=EffectiveDnsResponse)
async def get_effective_subnet_dns(
    subnet_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> EffectiveDnsResponse:
    """Resolve effective DNS settings for a subnet, walking up its block ancestry."""
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    if not subnet.dns_inherit_settings:
        return EffectiveDnsResponse(
            dns_group_ids=subnet.dns_group_ids or [],
            dns_zone_id=subnet.dns_zone_id,
            dns_additional_zone_ids=subnet.dns_additional_zone_ids or [],
            inherited_from_block_id=None,
        )

    current = await db.get(IPBlock, subnet.block_id) if subnet.block_id else None
    while current is not None:
        if not current.dns_inherit_settings:
            return EffectiveDnsResponse(
                dns_group_ids=current.dns_group_ids or [],
                dns_zone_id=current.dns_zone_id,
                dns_additional_zone_ids=current.dns_additional_zone_ids or [],
                inherited_from_block_id=str(current.id),
            )
        if current.parent_block_id:
            current = await db.get(IPBlock, current.parent_block_id)
        else:
            current = None

    return EffectiveDnsResponse(
        dns_group_ids=[], dns_zone_id=None, dns_additional_zone_ids=[], inherited_from_block_id=None
    )


@router.get("/subnets/{subnet_id}/effective-dhcp", response_model=EffectiveDhcpResponse)
async def get_effective_subnet_dhcp(
    subnet_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> EffectiveDhcpResponse:
    """Resolve effective DHCP server group for a subnet, walking up block ancestors
    and finally the space."""
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    if not subnet.dhcp_inherit_settings:
        return EffectiveDhcpResponse(
            dhcp_server_group_id=(
                str(subnet.dhcp_server_group_id) if subnet.dhcp_server_group_id else None
            ),
            inherited_from_block_id=None,
            inherited_from_space=False,
        )

    current = await db.get(IPBlock, subnet.block_id) if subnet.block_id else None
    while current is not None:
        if not current.dhcp_inherit_settings:
            return EffectiveDhcpResponse(
                dhcp_server_group_id=(
                    str(current.dhcp_server_group_id) if current.dhcp_server_group_id else None
                ),
                inherited_from_block_id=str(current.id),
                inherited_from_space=False,
            )
        if current.parent_block_id:
            current = await db.get(IPBlock, current.parent_block_id)
        else:
            # Fall through to space-level default when no block overrides.
            space = await db.get(IPSpace, current.space_id)
            if space and space.dhcp_server_group_id:
                return EffectiveDhcpResponse(
                    dhcp_server_group_id=str(space.dhcp_server_group_id),
                    inherited_from_block_id=None,
                    inherited_from_space=True,
                )
            current = None

    return EffectiveDhcpResponse(
        dhcp_server_group_id=None, inherited_from_block_id=None, inherited_from_space=False
    )


@router.put("/subnets/{subnet_id}", response_model=SubnetResponse)
async def update_subnet(
    subnet_id: uuid.UUID, body: SubnetUpdate, current_user: CurrentUser, db: DB
) -> Subnet:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    old_block_id = subnet.block_id

    # Validate reparent (block_id change): the target block must be in the same
    # space and the subnet CIDR must fit inside the target block's CIDR.
    if body.block_id is not None and body.block_id != subnet.block_id:
        target = await db.get(IPBlock, body.block_id)
        if target is None or target.space_id != subnet.space_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target block not found in this space",
            )
        subnet_net = _parse_network(str(subnet.network))
        target_net = _parse_network(str(target.network))
        if not subnet_net.subnet_of(target_net):  # type: ignore[arg-type]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Subnet {subnet.network} is not contained within block {target.network}",
            )

    # Validate new gateway is within the subnet
    if body.gateway is not None:
        try:
            gw = ipaddress.ip_address(body.gateway)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid gateway IP: {body.gateway}")
        net = _parse_network(str(subnet.network))
        if gw not in net:
            raise HTTPException(
                status_code=422,
                detail=f"Gateway {body.gateway} is not within subnet {subnet.network}",
            )

    # If vlan_ref_id is being set, derive/validate the integer vlan_id tag.
    if "vlan_ref_id" in body.model_fields_set:
        if body.vlan_ref_id is None:
            # Clearing the FK — leave vlan_id alone unless caller also sent it
            pass
        else:
            vlan_obj = await db.get(VLAN, body.vlan_ref_id)
            if vlan_obj is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Referenced VLAN not found"
                )
            if (
                "vlan_id" in body.model_fields_set
                and body.vlan_id is not None
                and body.vlan_id != vlan_obj.vlan_id
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"vlan_id ({body.vlan_id}) does not match the tag of the "
                        f"referenced VLAN ({vlan_obj.vlan_id})"
                    ),
                )
            body.vlan_id = vlan_obj.vlan_id

    old = {
        "name": subnet.name,
        "description": subnet.description,
        "gateway": str(subnet.gateway) if subnet.gateway else None,
        "status": subnet.status,
        "vlan_id": subnet.vlan_id,
        "vlan_ref_id": str(subnet.vlan_ref_id) if subnet.vlan_ref_id else None,
    }
    # setattr uses the raw Python values (UUID stays a UUID for the FK);
    # the audit log needs a JSON-safe projection so uuid.UUID → str.
    exclude_fields = {
        "manage_auto_addresses",
        "dns_group_ids",
        "dns_zone_id",
        "dns_additional_zone_ids",
        "dns_inherit_settings",
        "dhcp_server_group_id",
        "dhcp_inherit_settings",
    }
    changes = body.model_dump(exclude_none=True, exclude=exclude_fields)
    changes_for_audit = body.model_dump(mode="json", exclude_none=True, exclude=exclude_fields)
    for field, value in changes.items():
        setattr(subnet, field, value)
    # Handle DNS fields explicitly so boolean False and explicit null are preserved
    dns_fields = {"dns_group_ids", "dns_zone_id", "dns_additional_zone_ids", "dns_inherit_settings"}
    for field in dns_fields & body.model_fields_set:
        setattr(subnet, field, getattr(body, field))
    # Same treatment for DHCP fields.
    dhcp_fields = {"dhcp_server_group_id", "dhcp_inherit_settings"}
    for field in dhcp_fields & body.model_fields_set:
        val = getattr(body, field)
        setattr(subnet, field, val)
        changes_for_audit[field] = str(val) if isinstance(val, uuid.UUID) else val

    # Handle add/remove of auto-created network/broadcast/gateway records
    if body.manage_auto_addresses is not None:
        net = _parse_network(str(subnet.network))
        if net.prefixlen < 31:
            is_v6 = isinstance(net, ipaddress.IPv6Network)
            auto_statuses = {"network", "broadcast"}
            existing_result = await db.execute(
                select(IPAddress).where(
                    IPAddress.subnet_id == subnet.id,
                    IPAddress.status.in_(auto_statuses),
                )
            )
            existing_auto = existing_result.scalars().all()

            if body.manage_auto_addresses is False:
                # Add: create records that are missing
                existing_addrs = {str(a.address) for a in existing_auto}
                if str(net.network_address) not in existing_addrs:
                    db.add(
                        IPAddress(
                            subnet_id=subnet.id,
                            address=str(net.network_address),
                            status="network",
                            description="Network address",
                            created_by_user_id=current_user.id,
                        )
                    )
                # IPv6 has no broadcast — skip.
                if not is_v6 and str(net.broadcast_address) not in existing_addrs:
                    db.add(
                        IPAddress(
                            subnet_id=subnet.id,
                            address=str(net.broadcast_address),
                            status="broadcast",
                            description="Broadcast address",
                            created_by_user_id=current_user.id,
                        )
                    )
                await db.flush()
                await _update_utilization(db, subnet.id)
            else:
                # Remove: permanently delete network/broadcast records
                for addr in existing_auto:
                    await db.delete(addr)
                await db.flush()
                await _update_utilization(db, subnet.id)

    db.add(
        _audit(
            current_user,
            "update",
            "subnet",
            str(subnet.id),
            f"{subnet.network} ({subnet.name})",
            old_value=old,
            new_value=changes_for_audit,
        )
    )
    await db.flush()

    # Reparent: recalc utilization for both the old and the new block chains
    if old_block_id != subnet.block_id:
        await _update_block_utilization(db, old_block_id)
        await _update_block_utilization(db, subnet.block_id)

    await db.commit()
    await db.refresh(subnet)
    return subnet


@router.delete("/subnets/{subnet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subnet(subnet_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    from app.models.dns import DNSRecord, DNSZone

    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    block_id = subnet.block_id

    # Clean up DHCP artifacts first so downstream servers don't keep
    # serving leases for a range that no longer exists in IPAM.
    #
    #   * windows_dhcp (agentless) → push a remove-scope via WinRM before
    #     the DB row disappears; failure bubbles as 502 and rolls back.
    #   * kea / isc-dhcp (agent-based) → we can't actually reconfigure
    #     from the DB delete alone. Mark their config_etag dirty and
    #     enqueue an ``apply_config`` op so the next agent poll rebuilds
    #     the bundle without the removed scope.
    #
    # DHCPScope.subnet_id is ``ondelete=CASCADE`` so the rows themselves
    # (+ pools / statics via ORM cascade) will be cleaned automatically
    # when the subnet is deleted.
    scope_rows = (
        await db.execute(
            select(DHCPScope, DHCPServer)
            .join(DHCPServer, DHCPScope.server_id == DHCPServer.id, isouter=True)
            .where(DHCPScope.subnet_id == subnet_id)
        )
    ).all()
    agent_servers_to_refresh: dict[uuid.UUID, DHCPServer] = {}
    for scope, server in scope_rows:
        if server is None:
            continue
        if is_agentless(server.driver):
            await push_scope_delete(db, scope)
        else:
            agent_servers_to_refresh[server.id] = server

    # Clean up DNS artifacts so we don't leave orphaned records/zones behind.
    # IPAddress rows cascade-delete with the subnet, but DNSRecord.ip_address_id
    # is ON DELETE SET NULL, and auto-generated reverse zones keep linked_subnet_id
    # nulled instead of being removed.
    addr_result = await db.execute(
        select(IPAddress.dns_record_id).where(
            IPAddress.subnet_id == subnet_id,
            IPAddress.dns_record_id.isnot(None),
        )
    )
    record_ids = [rid for rid in addr_result.scalars().all() if rid is not None]
    if record_ids:
        await db.execute(delete(DNSRecord).where(DNSRecord.id.in_(record_ids)))

    await db.execute(
        delete(DNSZone).where(
            DNSZone.linked_subnet_id == subnet_id,
            DNSZone.is_auto_generated.is_(True),
        )
    )

    db.add(
        _audit(
            current_user,
            "delete",
            "subnet",
            str(subnet.id),
            f"{subnet.network} ({subnet.name})",
            old_value={"network": str(subnet.network), "name": subnet.name},
        )
    )
    await db.delete(subnet)
    await db.flush()
    await _update_block_utilization(db, block_id)

    # Rebuild bundles for any agent-based servers that lost a scope. Done
    # after the delete so the fresh bundle reflects the post-delete state.
    for server in agent_servers_to_refresh.values():
        bundle = await build_config_bundle(db, server)
        server.config_etag = bundle.etag
        existing = (
            await db.execute(
                select(DHCPConfigOp).where(
                    DHCPConfigOp.server_id == server.id,
                    DHCPConfigOp.op_type == "apply_config",
                    DHCPConfigOp.status == "pending",
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                DHCPConfigOp(
                    server_id=server.id,
                    op_type="apply_config",
                    payload={"etag": bundle.etag},
                    status="pending",
                )
            )

    await db.commit()


# ── Subnet + Block Resize (grow-only) ─────────────────────────────────────────
#
# Two endpoints per resource: ``/resize/preview`` (read-only blast-radius
# calculator) and ``/resize`` (commit under an advisory lock). The heavy
# lifting lives in ``app.services.ipam.resize``; handlers stay thin — parse
# the request, call the service, write the audit, commit, return.


class _ResizePlaceholderRow(BaseModel):
    ip: str
    hostname: str


class SubnetResizePreviewRequest(BaseModel):
    new_cidr: str
    # Included on preview so the service can surface a conflict when the
    # user asks to move the gateway on a CIDR with no usable host range
    # (/31/32/127/128). The UI forwards its checkbox state so "commit is
    # disabled because the ask is impossible" is visible before commit.
    move_gateway_to_first_usable: bool = False


class SubnetResizePreviewResponse(BaseModel):
    old_cidr: str
    new_cidr: str
    network_address_shifts: bool
    old_network_ip: str
    new_network_ip: str
    old_broadcast_ip: str | None
    new_broadcast_ip: str | None
    total_ips_before: int
    total_ips_after: int
    gateway_current: str | None
    gateway_suggested_new_first_usable: str | None
    placeholders_default_named: list[_ResizePlaceholderRow]
    placeholders_renamed: list[_ResizePlaceholderRow]
    affected_ip_addresses_total: int
    affected_dhcp_scopes: int
    affected_dhcp_pools: int
    affected_dhcp_static_assignments: int
    affected_dns_records_auto: int
    affected_active_leases: int
    reverse_zones_existing: list[str]
    reverse_zones_will_be_created: list[str]
    conflicts: list[dict[str, str]]
    warnings: list[str]


class SubnetResizeCommitRequest(BaseModel):
    new_cidr: str
    move_gateway_to_first_usable: bool = False
    replace_default_placeholders: bool = True


class SubnetResizeCommitResponse(BaseModel):
    subnet: SubnetResponse
    old_cidr: str
    new_cidr: str
    placeholders_deleted: int
    placeholders_created: int
    dhcp_servers_notified: int
    summary: list[str]


class BlockResizePreviewRequest(BaseModel):
    new_cidr: str


class _BlockResizeChildRow(BaseModel):
    id: str
    network: str
    name: str


class BlockResizePreviewResponse(BaseModel):
    old_cidr: str
    new_cidr: str
    network_address_shifts: bool
    old_network_ip: str
    new_network_ip: str
    total_ips_before: int
    total_ips_after: int
    child_blocks_count: int
    child_blocks: list[_BlockResizeChildRow]
    child_subnets_count: int
    child_subnets: list[_BlockResizeChildRow]
    descendant_ip_addresses_total: int
    conflicts: list[dict[str, str]]
    warnings: list[str]


class BlockResizeCommitRequest(BaseModel):
    new_cidr: str


class BlockResizeCommitResponse(BaseModel):
    block: IPBlockResponse
    old_cidr: str
    new_cidr: str
    summary: list[str]


@router.post(
    "/subnets/{subnet_id}/resize/preview",
    response_model=SubnetResizePreviewResponse,
)
async def resize_subnet_preview(
    subnet_id: uuid.UUID,
    body: SubnetResizePreviewRequest,
    current_user: CurrentUser,
    db: DB,
) -> SubnetResizePreviewResponse:
    from app.services.ipam.resize import preview_subnet_resize

    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")
    preview = await preview_subnet_resize(
        db,
        subnet,
        body.new_cidr,
        move_gateway_to_first_usable=body.move_gateway_to_first_usable,
    )
    return SubnetResizePreviewResponse(
        old_cidr=preview.old_cidr,
        new_cidr=preview.new_cidr,
        network_address_shifts=preview.network_address_shifts,
        old_network_ip=preview.old_network_ip,
        new_network_ip=preview.new_network_ip,
        old_broadcast_ip=preview.old_broadcast_ip,
        new_broadcast_ip=preview.new_broadcast_ip,
        total_ips_before=preview.total_ips_before,
        total_ips_after=preview.total_ips_after,
        gateway_current=preview.gateway_current,
        gateway_suggested_new_first_usable=preview.gateway_suggested_new_first_usable,
        placeholders_default_named=[
            _ResizePlaceholderRow(**p) for p in preview.placeholders_default_named
        ],
        placeholders_renamed=[_ResizePlaceholderRow(**p) for p in preview.placeholders_renamed],
        affected_ip_addresses_total=preview.affected_ip_addresses_total,
        affected_dhcp_scopes=preview.affected_dhcp_scopes,
        affected_dhcp_pools=preview.affected_dhcp_pools,
        affected_dhcp_static_assignments=preview.affected_dhcp_static_assignments,
        affected_dns_records_auto=preview.affected_dns_records_auto,
        affected_active_leases=preview.affected_active_leases,
        reverse_zones_existing=preview.reverse_zones_existing,
        reverse_zones_will_be_created=preview.reverse_zones_will_be_created,
        conflicts=[{"type": c.type, "detail": c.detail} for c in preview.conflicts],
        warnings=preview.warnings,
    )


@router.post("/subnets/{subnet_id}/resize", response_model=SubnetResizeCommitResponse)
async def resize_subnet_commit(
    subnet_id: uuid.UUID,
    body: SubnetResizeCommitRequest,
    current_user: CurrentUser,
    db: DB,
) -> SubnetResizeCommitResponse:
    from app.services.ipam.resize import ResizeError, commit_subnet_resize

    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    old_snapshot = {
        "network": str(subnet.network),
        "gateway": str(subnet.gateway) if subnet.gateway else None,
        "total_ips": subnet.total_ips,
    }

    try:
        result = await commit_subnet_resize(
            db,
            subnet,
            body.new_cidr,
            move_gateway_to_first_usable=body.move_gateway_to_first_usable,
            replace_default_placeholders=body.replace_default_placeholders,
            current_user=current_user,
        )
    except ResizeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    db.add(
        _audit(
            current_user,
            "resize",
            "subnet",
            str(subnet.id),
            f"{result.old_cidr} → {result.new_cidr}",
            old_value=old_snapshot,
            new_value={
                "network": result.new_cidr,
                "gateway": str(subnet.gateway) if subnet.gateway else None,
                "total_ips": subnet.total_ips,
                "reason": "user_resize",
                "placeholders_deleted": result.placeholders_deleted,
                "placeholders_created": result.placeholders_created,
                "dhcp_servers_notified": result.dhcp_servers_notified,
            },
        )
    )

    await db.commit()
    await db.refresh(subnet)
    return SubnetResizeCommitResponse(
        subnet=SubnetResponse.model_validate(subnet),
        old_cidr=result.old_cidr,
        new_cidr=result.new_cidr,
        placeholders_deleted=result.placeholders_deleted,
        placeholders_created=result.placeholders_created,
        dhcp_servers_notified=result.dhcp_servers_notified,
        summary=result.summary,
    )


@router.post(
    "/blocks/{block_id}/resize/preview",
    response_model=BlockResizePreviewResponse,
)
async def resize_block_preview(
    block_id: uuid.UUID,
    body: BlockResizePreviewRequest,
    current_user: CurrentUser,
    db: DB,
) -> BlockResizePreviewResponse:
    from app.services.ipam.resize import preview_block_resize

    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP block not found")
    preview = await preview_block_resize(db, block, body.new_cidr)
    return BlockResizePreviewResponse(
        old_cidr=preview.old_cidr,
        new_cidr=preview.new_cidr,
        network_address_shifts=preview.network_address_shifts,
        old_network_ip=preview.old_network_ip,
        new_network_ip=preview.new_network_ip,
        total_ips_before=preview.total_ips_before,
        total_ips_after=preview.total_ips_after,
        child_blocks_count=preview.child_blocks_count,
        child_blocks=[_BlockResizeChildRow(**c) for c in preview.child_blocks],
        child_subnets_count=preview.child_subnets_count,
        child_subnets=[_BlockResizeChildRow(**c) for c in preview.child_subnets],
        descendant_ip_addresses_total=preview.descendant_ip_addresses_total,
        conflicts=[{"type": c.type, "detail": c.detail} for c in preview.conflicts],
        warnings=preview.warnings,
    )


@router.post("/blocks/{block_id}/resize", response_model=BlockResizeCommitResponse)
async def resize_block_commit(
    block_id: uuid.UUID,
    body: BlockResizeCommitRequest,
    current_user: CurrentUser,
    db: DB,
) -> BlockResizeCommitResponse:
    from app.services.ipam.resize import ResizeError, commit_block_resize

    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP block not found")

    old_snapshot = {"network": str(block.network)}

    try:
        result = await commit_block_resize(db, block, body.new_cidr, current_user=current_user)
    except ResizeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    db.add(
        _audit(
            current_user,
            "resize",
            "ip_block",
            str(block.id),
            f"{result.old_cidr} → {result.new_cidr}",
            old_value=old_snapshot,
            new_value={"network": result.new_cidr, "reason": "user_resize"},
        )
    )

    await db.commit()
    await db.refresh(block)
    return BlockResizeCommitResponse(
        block=IPBlockResponse.model_validate(block),
        old_cidr=result.old_cidr,
        new_cidr=result.new_cidr,
        summary=result.summary,
    )


# ── Subnet ↔ DNS sync (drift detection + reconcile) ───────────────────────────


class _DnsSyncMissingResp(BaseModel):
    ip_id: uuid.UUID
    ip_address: str
    hostname: str
    record_type: str
    expected_name: str
    expected_value: str
    zone_id: uuid.UUID
    zone_name: str


class _DnsSyncMismatchResp(BaseModel):
    record_id: uuid.UUID
    ip_id: uuid.UUID
    ip_address: str
    record_type: str
    zone_id: uuid.UUID
    zone_name: str
    current_name: str
    current_value: str
    expected_name: str
    expected_value: str


class _DnsSyncStaleResp(BaseModel):
    record_id: uuid.UUID
    record_type: str
    zone_id: uuid.UUID
    zone_name: str
    name: str
    value: str
    reason: str


class DnsSyncPreviewResponse(BaseModel):
    subnet_id: uuid.UUID
    forward_zone_id: uuid.UUID | None
    forward_zone_name: str | None
    reverse_zone_id: uuid.UUID | None
    reverse_zone_name: str | None
    missing: list[_DnsSyncMissingResp]
    mismatched: list[_DnsSyncMismatchResp]
    stale: list[_DnsSyncStaleResp]


class DnsSyncCommitRequest(BaseModel):
    """Lists of IP IDs and DNS record IDs the user wants to act on.
    Anything omitted is left alone — we never auto-fix the whole report."""

    create_for_ip_ids: list[uuid.UUID] = []
    update_record_ids: list[uuid.UUID] = []
    delete_stale_record_ids: list[uuid.UUID] = []


class DnsSyncCommitResponse(BaseModel):
    created: int
    updated: int
    deleted: int
    errors: list[str]


class DnsSyncSummaryResponse(BaseModel):
    """Compact drift counts — cheap to poll from the subnet header so the
    UI can flag "N records out of sync" without rendering the full
    per-row preview. Internally runs the same ``compute_subnet_dns_drift``
    as the preview endpoint."""

    subnet_id: uuid.UUID
    missing: int
    mismatched: int
    stale: int
    total: int
    has_drift: bool


@router.get(
    "/subnets/{subnet_id}/dns-sync/summary",
    response_model=DnsSyncSummaryResponse,
)
async def dns_sync_summary(
    subnet_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> DnsSyncSummaryResponse:
    """Counts-only drift report for surfacing a "you have N stale records"
    banner on the subnet header without the per-row payload cost."""
    from app.services.dns.sync_check import compute_subnet_dns_drift  # noqa: PLC0415

    if await db.get(Subnet, subnet_id) is None:
        raise HTTPException(status_code=404, detail="Subnet not found")
    report = await compute_subnet_dns_drift(db, subnet_id)
    missing = len(report.missing)
    mismatched = len(report.mismatched)
    stale = len(report.stale)
    total = missing + mismatched + stale
    return DnsSyncSummaryResponse(
        subnet_id=subnet_id,
        missing=missing,
        mismatched=mismatched,
        stale=stale,
        total=total,
        has_drift=total > 0,
    )


@router.get(
    "/subnets/{subnet_id}/dns-sync/preview",
    response_model=DnsSyncPreviewResponse,
)
async def dns_sync_preview(
    subnet_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> DnsSyncPreviewResponse:
    """Return a drift report comparing IPAM-expected DNS records to the actual
    rows in the DB. Read-only — does not push anything to BIND."""
    from app.services.dns.sync_check import compute_subnet_dns_drift

    if await db.get(Subnet, subnet_id) is None:
        raise HTTPException(status_code=404, detail="Subnet not found")
    report = await compute_subnet_dns_drift(db, subnet_id)
    return DnsSyncPreviewResponse(
        subnet_id=report.subnet_id,
        forward_zone_id=report.forward_zone_id,
        forward_zone_name=report.forward_zone_name,
        reverse_zone_id=report.reverse_zone_id,
        reverse_zone_name=report.reverse_zone_name,
        missing=[_DnsSyncMissingResp(**m.__dict__) for m in report.missing],
        mismatched=[_DnsSyncMismatchResp(**m.__dict__) for m in report.mismatched],
        stale=[_DnsSyncStaleResp(**s.__dict__) for s in report.stale],
    )


async def _apply_dns_sync(
    db: AsyncSession,
    body: DnsSyncCommitRequest,
    *,
    restrict_subnet_id: uuid.UUID | None = None,
) -> tuple[int, int, int, list[str]]:
    """Apply create/update/delete actions, looking up each IP's owning subnet
    on the fly. ``restrict_subnet_id`` scopes the create/update set to a
    single subnet (used by the per-subnet endpoint); aggregate endpoints
    leave it None."""
    created = 0
    updated = 0
    deleted = 0
    errors: list[str] = []

    ip_ids_to_sync: set[uuid.UUID] = set(body.create_for_ip_ids)
    if body.update_record_ids:
        rec_res = await db.execute(
            select(DNSRecord.id, DNSRecord.ip_address_id).where(
                DNSRecord.id.in_(body.update_record_ids)
            )
        )
        for _rid, ip_id in rec_res.all():
            if ip_id is not None:
                ip_ids_to_sync.add(ip_id)

    if ip_ids_to_sync:
        q = select(IPAddress).where(IPAddress.id.in_(ip_ids_to_sync))
        if restrict_subnet_id is not None:
            q = q.where(IPAddress.subnet_id == restrict_subnet_id)
        ips_res = await db.execute(q)
        # Cache subnets so we don't re-fetch per IP in the same subnet.
        subnet_cache: dict[uuid.UUID, Subnet | None] = {}
        for ip in ips_res.scalars().all():
            sn = subnet_cache.get(ip.subnet_id)
            if sn is None and ip.subnet_id not in subnet_cache:
                sn = await db.get(Subnet, ip.subnet_id)
                subnet_cache[ip.subnet_id] = sn
            if sn is None:
                errors.append(f"{ip.address}: parent subnet missing")
                continue
            try:
                await _sync_dns_record(db, ip, sn, action="create")
                if ip.id in body.create_for_ip_ids:
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                errors.append(f"{ip.address}: {exc}")

    if body.delete_stale_record_ids:
        from app.services.dns.record_ops import (  # noqa: PLC0415
            enqueue_record_ops_batch,
        )

        stale_res = await db.execute(
            select(DNSRecord)
            .where(
                DNSRecord.id.in_(body.delete_stale_record_ids),
                DNSRecord.auto_generated.is_(True),
            )
            .options(selectinload(DNSRecord.zone))
        )
        stale_records = list(stale_res.scalars().all())

        # Group by zone so each zone's primary server gets a single
        # batched driver call (critical for agentless Windows DNS — one
        # WinRM round trip per zone instead of one per record).
        by_zone: dict[uuid.UUID, list[DNSRecord]] = {}
        zones_by_id: dict[uuid.UUID, Any] = {}
        orphans: list[DNSRecord] = []  # records whose zone link is gone
        for rec in stale_records:
            if rec.zone is None:
                orphans.append(rec)
                continue
            by_zone.setdefault(rec.zone_id, []).append(rec)
            zones_by_id[rec.zone_id] = rec.zone

        # Preload every linked IP up front so cache invalidation below
        # doesn't fire N per-record fetches after the WinRM round trip.
        ip_ids = {rec.ip_address_id for rec in stale_records if rec.ip_address_id is not None}
        ips_by_id: dict[uuid.UUID, IPAddress] = {}
        if ip_ids:
            ips_res = await db.execute(select(IPAddress).where(IPAddress.id.in_(ip_ids)))
            ips_by_id = {ip.id: ip for ip in ips_res.scalars().all()}

        for zone_id, recs in by_zone.items():
            zone = zones_by_id[zone_id]
            ops = [
                {
                    "op": "delete",
                    "record": {
                        "name": r.name,
                        "type": r.record_type,
                        "value": r.value,
                        "ttl": r.ttl,
                    },
                }
                for r in recs
            ]
            try:
                op_rows = await enqueue_record_ops_batch(db, zone, ops)
            except Exception as exc:  # noqa: BLE001 — whole-batch failure
                errors.append(f"batch delete on {zone.name}: {exc}")
                continue

            # Honor per-op state. A failed wire op must NOT remove the
            # DB row — otherwise the UI tells the user "deleted" but the
            # record is still published on the DNS server, and a later
            # "Sync with server" pulls the zombie back into IPAM. Only
            # state=="applied" (or a zone-less orphan with no wire op to
            # begin with) is safe to delete locally.
            for r, op_row in zip(recs, op_rows, strict=True):
                if op_row is None:
                    errors.append(
                        f"{r.record_type} {r.name}.{zone.name}: "
                        "no primary configured for zone — wire delete skipped"
                    )
                    continue
                if op_row.state != "applied":
                    errors.append(
                        f"{r.record_type} {r.name}.{zone.name}: "
                        f"wire delete failed — {op_row.last_error or 'unknown'}"
                    )
                    continue
                try:
                    linked_ip = (
                        ips_by_id.get(r.ip_address_id) if r.ip_address_id is not None else None
                    )
                    _invalidate_ip_dns_cache(r, linked_ip)
                    await db.delete(r)
                    deleted += 1
                except Exception as exc:
                    errors.append(f"record {r.id}: {exc}")

        # Zone-less stragglers: just drop the DB row (no wire op possible).
        for rec in orphans:
            try:
                linked_ip = (
                    ips_by_id.get(rec.ip_address_id) if rec.ip_address_id is not None else None
                )
                _invalidate_ip_dns_cache(rec, linked_ip)
                await db.delete(rec)
                deleted += 1
            except Exception as exc:
                errors.append(f"record {rec.id}: {exc}")

    return created, updated, deleted, errors


@router.post(
    "/subnets/{subnet_id}/dns-sync/commit",
    response_model=DnsSyncCommitResponse,
)
async def dns_sync_commit(
    subnet_id: uuid.UUID,
    body: DnsSyncCommitRequest,
    current_user: CurrentUser,
    db: DB,
) -> DnsSyncCommitResponse:
    """Apply the user-selected drift actions for one subnet. Anything not
    listed is skipped."""
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=404, detail="Subnet not found")

    created, updated, deleted, errors = await _apply_dns_sync(
        db,
        body,
        restrict_subnet_id=subnet_id,
    )

    if created or updated or deleted:
        db.add(
            _audit(
                current_user,
                "dns-sync",
                "subnet",
                str(subnet.id),
                f"{subnet.network}",
                new_value={
                    "created": created,
                    "updated": updated,
                    "deleted": deleted,
                    "errors": errors,
                },
            )
        )
    await db.commit()
    return DnsSyncCommitResponse(created=created, updated=updated, deleted=deleted, errors=errors)


@router.get(
    "/blocks/{block_id}/dns-sync/preview",
    response_model=DnsSyncPreviewResponse,
)
async def dns_sync_preview_block(
    block_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> DnsSyncPreviewResponse:
    from app.services.dns.sync_check import compute_block_dns_drift

    if await db.get(IPBlock, block_id) is None:
        raise HTTPException(status_code=404, detail="Block not found")
    report = await compute_block_dns_drift(db, block_id)
    return DnsSyncPreviewResponse(
        subnet_id=report.subnet_id,
        forward_zone_id=None,
        forward_zone_name=None,
        reverse_zone_id=None,
        reverse_zone_name=None,
        missing=[_DnsSyncMissingResp(**m.__dict__) for m in report.missing],
        mismatched=[_DnsSyncMismatchResp(**m.__dict__) for m in report.mismatched],
        stale=[_DnsSyncStaleResp(**s.__dict__) for s in report.stale],
    )


@router.post(
    "/blocks/{block_id}/dns-sync/commit",
    response_model=DnsSyncCommitResponse,
)
async def dns_sync_commit_block(
    block_id: uuid.UUID,
    body: DnsSyncCommitRequest,
    current_user: CurrentUser,
    db: DB,
) -> DnsSyncCommitResponse:
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=404, detail="Block not found")
    created, updated, deleted, errors = await _apply_dns_sync(db, body)
    if created or updated or deleted:
        db.add(
            _audit(
                current_user,
                "dns-sync",
                "block",
                str(block.id),
                block.network,
                new_value={
                    "created": created,
                    "updated": updated,
                    "deleted": deleted,
                    "errors": errors,
                },
            )
        )
    await db.commit()
    return DnsSyncCommitResponse(created=created, updated=updated, deleted=deleted, errors=errors)


@router.get(
    "/spaces/{space_id}/dns-sync/preview",
    response_model=DnsSyncPreviewResponse,
)
async def dns_sync_preview_space(
    space_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> DnsSyncPreviewResponse:
    from app.services.dns.sync_check import compute_space_dns_drift

    if await db.get(IPSpace, space_id) is None:
        raise HTTPException(status_code=404, detail="Space not found")
    report = await compute_space_dns_drift(db, space_id)
    return DnsSyncPreviewResponse(
        subnet_id=report.subnet_id,
        forward_zone_id=None,
        forward_zone_name=None,
        reverse_zone_id=None,
        reverse_zone_name=None,
        missing=[_DnsSyncMissingResp(**m.__dict__) for m in report.missing],
        mismatched=[_DnsSyncMismatchResp(**m.__dict__) for m in report.mismatched],
        stale=[_DnsSyncStaleResp(**s.__dict__) for s in report.stale],
    )


@router.post(
    "/spaces/{space_id}/dns-sync/commit",
    response_model=DnsSyncCommitResponse,
)
async def dns_sync_commit_space(
    space_id: uuid.UUID,
    body: DnsSyncCommitRequest,
    current_user: CurrentUser,
    db: DB,
) -> DnsSyncCommitResponse:
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=404, detail="Space not found")
    created, updated, deleted, errors = await _apply_dns_sync(db, body)
    if created or updated or deleted:
        db.add(
            _audit(
                current_user,
                "dns-sync",
                "space",
                str(space.id),
                space.name,
                new_value={
                    "created": created,
                    "updated": updated,
                    "deleted": deleted,
                    "errors": errors,
                },
            )
        )
    await db.commit()
    return DnsSyncCommitResponse(created=created, updated=updated, deleted=deleted, errors=errors)


# ── Reverse-zone backfill ──────────────────────────────────────────────────────
#
# Subnets created before DNS was assigned never had their matching in-addr.arpa
# / ip6.arpa zone auto-created. Rather than delete+recreate the subnet, the
# operator can call these endpoints to create any missing reverse zones in
# bulk. Idempotent — skips subnets whose reverse zone already exists.


class BackfillReverseZonesResponse(BaseModel):
    created: list[dict[str, str]] = []  # [{"subnet": "10.1.0.0/24", "zone": "0.1.10.in-addr.arpa"}]
    skipped: int = 0  # subnets that already had a reverse zone or no DNS group


async def _backfill_reverse_zones(
    db: AsyncSession, subnets: list[Subnet], user: Any
) -> BackfillReverseZonesResponse:
    from app.services.dns.reverse_zone import (
        compute_reverse_zone_name,
        ensure_reverse_zone_for_subnet,
    )

    resp = BackfillReverseZonesResponse()
    for s in subnets:
        # Pre-check: does a reverse zone already exist for this network in
        # any group? If so, it's not a candidate for backfill.
        try:
            expected_name = compute_reverse_zone_name(str(s.network))
        except Exception:  # noqa: BLE001
            resp.skipped += 1
            continue
        pre = await db.execute(
            select(DNSZone).where(DNSZone.name == expected_name, DNSZone.kind == "reverse")
        )
        if pre.scalar_one_or_none() is not None:
            resp.skipped += 1
            continue
        try:
            zone = await ensure_reverse_zone_for_subnet(db, s, user)
        except Exception:  # noqa: BLE001
            resp.skipped += 1
            continue
        if zone is not None:
            resp.created.append({"subnet": str(s.network), "zone": zone.name})
        else:
            # Subnet has no effective DNS group → nothing to do.
            resp.skipped += 1
    return resp


@router.post(
    "/subnets/{subnet_id}/reverse-zones/backfill",
    response_model=BackfillReverseZonesResponse,
)
async def backfill_reverse_zones_subnet(
    subnet_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> BackfillReverseZonesResponse:
    s = await db.get(Subnet, subnet_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Subnet not found")
    resp = await _backfill_reverse_zones(db, [s], current_user)
    await db.commit()
    return resp


@router.post(
    "/blocks/{block_id}/reverse-zones/backfill",
    response_model=BackfillReverseZonesResponse,
)
async def backfill_reverse_zones_block(
    block_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> BackfillReverseZonesResponse:
    # Walk the block subtree (block + descendant blocks' subnets)
    block_ids: set[uuid.UUID] = {block_id}
    pending = [block_id]
    while pending:
        parent = pending.pop()
        res = await db.execute(select(IPBlock).where(IPBlock.parent_block_id == parent))
        for b in res.scalars().all():
            block_ids.add(b.id)
            pending.append(b.id)
    subs_res = await db.execute(select(Subnet).where(Subnet.block_id.in_(block_ids)))
    subnets = list(subs_res.scalars().all())
    resp = await _backfill_reverse_zones(db, subnets, current_user)
    await db.commit()
    return resp


@router.post(
    "/spaces/{space_id}/reverse-zones/backfill",
    response_model=BackfillReverseZonesResponse,
)
async def backfill_reverse_zones_space(
    space_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> BackfillReverseZonesResponse:
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=404, detail="Space not found")
    subs_res = await db.execute(select(Subnet).where(Subnet.space_id == space_id))
    subnets = list(subs_res.scalars().all())
    resp = await _backfill_reverse_zones(db, subnets, current_user)
    await db.commit()
    return resp


# ── IP Addresses ───────────────────────────────────────────────────────────────


async def _alias_counts_for(db: AsyncSession, ips: list[IPAddress]) -> dict[uuid.UUID, int]:
    """Return {ip_id: alias_count} excluding each IP's primary A record."""
    if not ips:
        return {}
    ip_ids = [ip.id for ip in ips]
    primary_ids = {ip.dns_record_id for ip in ips if ip.dns_record_id is not None}
    conds = [
        DNSRecord.ip_address_id.in_(ip_ids),
        DNSRecord.auto_generated.is_(True),
        DNSRecord.record_type.in_(["CNAME", "A"]),
    ]
    if primary_ids:
        conds.append(DNSRecord.id.notin_(primary_ids))
    q = (
        select(DNSRecord.ip_address_id, func.count(DNSRecord.id))
        .where(*conds)
        .group_by(DNSRecord.ip_address_id)
    )
    return {row[0]: row[1] for row in (await db.execute(q)).all()}


@router.get("/subnets/{subnet_id}/addresses", response_model=list[IPAddressResponse])
async def list_addresses(
    subnet_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    status_filter: str | None = None,
) -> list[IPAddress]:
    if await db.get(Subnet, subnet_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    query = (
        select(IPAddress)
        .where(IPAddress.subnet_id == subnet_id)
        .order_by(text("CAST(address AS inet)"))
    )
    if status_filter:
        query = query.where(IPAddress.status == status_filter)
    rows = list((await db.execute(query)).scalars().all())
    counts = await _alias_counts_for(db, rows)
    for ip in rows:
        ip.alias_count = counts.get(ip.id, 0)  # type: ignore[attr-defined]
    return rows


@router.post(
    "/subnets/{subnet_id}/addresses",
    response_model=IPAddressResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_address(
    subnet_id: uuid.UUID, body: IPAddressCreate, current_user: CurrentUser, db: DB
) -> IPAddress:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    # Validate address belongs to subnet
    try:
        addr = ipaddress.ip_address(body.address)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid IP address: {body.address}")

    net = _parse_network(str(subnet.network))
    if addr not in net:
        raise HTTPException(
            status_code=422,
            detail=f"Address {body.address} is not within subnet {subnet.network}",
        )

    # MAC address required for static_dhcp
    if body.status == "static_dhcp" and not body.mac_address:
        raise HTTPException(
            status_code=422,
            detail="mac_address is required when status is 'static_dhcp'",
        )

    # Check address not already in use
    existing = await db.scalar(
        select(func.count())
        .select_from(IPAddress)
        .where(IPAddress.subnet_id == subnet_id)
        .where(IPAddress.address == body.address)
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Address {body.address} is already allocated in this subnet",
        )

    # Resolve the zone that WILL be used by _sync_dns_record so the collision
    # check sees the same forward_zone_id that will land on the row.
    explicit_zone = uuid.UUID(body.dns_zone_id) if body.dns_zone_id else None
    effective_zone = explicit_zone or await _resolve_effective_zone(db, subnet)
    if not body.force:
        warnings = await _check_ip_collisions(
            db,
            hostname=body.hostname,
            forward_zone_id=effective_zone,
            mac_address=body.mac_address,
        )
        if warnings:
            raise _collision_http_exc(warnings)

    ip = IPAddress(
        subnet_id=subnet_id,
        created_by_user_id=current_user.id,
        **body.model_dump(exclude={"dns_zone_id", "force"}),
    )
    db.add(ip)
    await db.flush()

    # Sync DNS A record
    await _sync_dns_record(db, ip, subnet, zone_id=explicit_zone, action="create")
    # User-specified alias records (CNAME / A) tied to this IP
    if body.aliases:
        await _create_alias_records(db, ip, subnet, body.aliases, zone_id=explicit_zone)

    db.add(
        _audit(
            current_user,
            "create",
            "ip_address",
            str(ip.id),
            body.address,
            new_value=body.model_dump(mode="json", exclude={"dns_zone_id", "force"}),
        )
    )
    await db.flush()
    await _update_utilization(db, subnet_id)
    await _update_block_utilization(db, subnet.block_id)
    await db.commit()
    await db.refresh(ip)
    logger.info(
        "ip_address_created", ip_id=str(ip.id), address=body.address, subnet_id=str(subnet_id)
    )
    return ip


@router.get("/addresses/{address_id}", response_model=IPAddressResponse)
async def get_address(address_id: uuid.UUID, current_user: CurrentUser, db: DB) -> IPAddress:
    ip = await db.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP address not found")
    return ip


@router.put("/addresses/{address_id}", response_model=IPAddressResponse)
async def update_address(
    address_id: uuid.UUID, body: IPAddressUpdate, current_user: CurrentUser, db: DB
) -> IPAddress:
    ip = await db.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP address not found")

    # Dynamic-lease mirrors are owned by the DHCP server, not IPAM. Any edit
    # here would be overwritten on the next pull cycle, so refuse outright —
    # the user needs to edit the lease / reservation at the source.
    if ip.auto_from_lease:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This IP mirrors a dynamic DHCP lease and is managed by the "
                "DHCP server. Edit the lease or convert it to a reservation "
                "at the source."
            ),
        )

    # MAC required if transitioning to static_dhcp
    new_status = body.status or ip.status
    new_mac = body.mac_address if body.mac_address is not None else ip.mac_address
    if new_status == "static_dhcp" and not new_mac:
        raise HTTPException(
            status_code=422,
            detail="mac_address is required when status is 'static_dhcp'",
        )

    old = {
        "status": ip.status,
        "hostname": ip.hostname,
        "mac_address": str(ip.mac_address) if ip.mac_address else None,
    }
    old_status = ip.status

    # Collision check — only on fields the client actually touched. We use
    # ``exclude_unset`` rather than value-equality so an unchanged field
    # never surfaces a warning, even if an existing cross-subnet collision
    # would match it. The ``exclude_ip_id`` filter prevents this IP from
    # colliding with its own current state.
    touched = body.model_dump(exclude_unset=True)
    hostname_or_zone_touched = "hostname" in touched or "dns_zone_id" in touched
    mac_touched = "mac_address" in touched
    if not body.force and (hostname_or_zone_touched or mac_touched):
        subnet_for_check = await db.get(Subnet, ip.subnet_id)
        new_hostname = body.hostname if body.hostname is not None else ip.hostname
        if "dns_zone_id" in touched:
            new_zone_id = uuid.UUID(body.dns_zone_id) if body.dns_zone_id else None
        else:
            new_zone_id = ip.forward_zone_id or (
                await _resolve_effective_zone(db, subnet_for_check) if subnet_for_check else None
            )
        warnings = await _check_ip_collisions(
            db,
            hostname=new_hostname if hostname_or_zone_touched else None,
            forward_zone_id=new_zone_id if hostname_or_zone_touched else None,
            mac_address=body.mac_address if mac_touched else None,
            exclude_ip_id=ip.id,
        )
        if warnings:
            raise _collision_http_exc(warnings)

    changes = body.model_dump(exclude_none=True, exclude={"dns_zone_id", "force"})
    changes_for_audit = body.model_dump(
        mode="json", exclude_none=True, exclude={"dns_zone_id", "force"}
    )
    for field, value in changes.items():
        setattr(ip, field, value)

    # Sync DNS:
    # - hostname or dns_zone_id changed → update
    # - status flipped from 'orphan' to a live state AND we have a remembered
    #   forward_zone_id from before the soft-delete → restore the records
    subnet = await db.get(Subnet, ip.subnet_id)
    restoring = (
        old_status == "orphan"
        and ip.status != "orphan"
        and ip.hostname
        and ip.forward_zone_id is not None
    )
    if subnet and ("hostname" in changes or body.dns_zone_id is not None):
        zone_id = uuid.UUID(body.dns_zone_id) if body.dns_zone_id else None
        await _sync_dns_record(db, ip, subnet, zone_id=zone_id, action="update")
    elif subnet and restoring:
        await _sync_dns_record(db, ip, subnet, zone_id=ip.forward_zone_id, action="create")

    db.add(
        _audit(
            current_user,
            "update",
            "ip_address",
            str(ip.id),
            str(ip.address),
            old_value=old,
            new_value=changes_for_audit,
        )
    )

    # Update utilization if status changed (available ↔ non-available)
    status_was_available = old_status == "available"
    status_now_available = ip.status == "available"
    if status_was_available != status_now_available:
        await db.flush()
        if subnet:
            await _update_utilization(db, ip.subnet_id)
            await _update_block_utilization(db, subnet.block_id)

    await db.commit()
    await db.refresh(ip)
    return ip


class AliasResponse(BaseModel):
    id: uuid.UUID
    name: str
    record_type: str
    value: str
    zone_id: uuid.UUID
    fqdn: str

    model_config = {"from_attributes": True}


@router.get("/addresses/{address_id}/aliases", response_model=list[AliasResponse])
async def list_aliases(address_id: uuid.UUID, current_user: CurrentUser, db: DB) -> list[DNSRecord]:
    ip = await db.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=404, detail="IP address not found")
    # Aliases are auto-generated records linked to this IP that aren't the
    # primary forward A (which is stored separately on ip.dns_record_id).
    res = await db.execute(
        select(DNSRecord).where(
            DNSRecord.ip_address_id == ip.id,
            DNSRecord.auto_generated.is_(True),
            DNSRecord.record_type.in_(["CNAME", "A"]),
        )
    )
    out = []
    for r in res.scalars().all():
        if ip.dns_record_id is not None and r.id == ip.dns_record_id:
            continue  # exclude the primary A
        out.append(r)
    return out


@router.post(
    "/addresses/{address_id}/aliases",
    response_model=AliasResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_alias(
    address_id: uuid.UUID, body: AliasInput, current_user: CurrentUser, db: DB
) -> DNSRecord:
    ip = await db.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=404, detail="IP address not found")
    subnet = await db.get(Subnet, ip.subnet_id)
    if subnet is None:
        raise HTTPException(status_code=404, detail="Subnet not found")
    zone_id = ip.forward_zone_id or await _resolve_effective_zone(db, subnet)
    if not zone_id:
        raise HTTPException(
            status_code=409,
            detail="No DNS zone configured for this subnet — add one first.",
        )
    await _create_alias_records(db, ip, subnet, [body], zone_id=zone_id)
    # Find the just-created record
    res = await db.execute(
        select(DNSRecord).where(
            DNSRecord.zone_id == zone_id,
            DNSRecord.name == body.name,
            DNSRecord.record_type == body.record_type,
            DNSRecord.ip_address_id == ip.id,
        )
    )
    rec = res.scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=409, detail="Alias already exists or failed to create")
    db.add(
        _audit(
            current_user,
            "create",
            "dns_record",
            str(rec.id),
            rec.fqdn,
            new_value={
                "name": rec.name,
                "record_type": rec.record_type,
                "value": rec.value,
                "alias_of": str(ip.id),
            },
        )
    )
    await db.commit()
    await db.refresh(rec)
    return rec


@router.delete(
    "/addresses/{address_id}/aliases/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_alias(
    address_id: uuid.UUID,
    record_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
) -> None:
    ip = await db.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=404, detail="IP address not found")
    rec = await db.get(DNSRecord, record_id)
    if rec is None or rec.ip_address_id != ip.id:
        raise HTTPException(status_code=404, detail="Alias not found")
    if ip.dns_record_id == rec.id:
        raise HTTPException(
            status_code=409,
            detail="Can't delete the primary A record — change the IP's hostname or delete the IP instead.",
        )
    zone = await db.get(DNSZone, rec.zone_id)
    if zone is not None:
        await _enqueue_dns_op(db, zone, "delete", rec.name, rec.record_type, rec.value, rec.ttl)
    db.add(
        _audit(
            current_user,
            "delete",
            "dns_record",
            str(rec.id),
            rec.fqdn,
            old_value={"name": rec.name, "record_type": rec.record_type, "value": rec.value},
        )
    )
    await db.delete(rec)
    await db.commit()


@router.delete("/addresses/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(
    address_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
    permanent: bool = Query(
        default=False, description="Permanently delete instead of marking orphan"
    ),
) -> None:
    ip = await db.get(IPAddress, address_id)
    if ip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP address not found")

    # Dynamic-lease mirrors are owned by the DHCP server. Deleting one here
    # would just get recreated on the next pull cycle — block it so the user
    # sees why and goes to release the lease at the source. (The lease-pull
    # task deletes these rows via its own SessionLocal, not this endpoint.)
    if ip.auto_from_lease:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This IP mirrors a dynamic DHCP lease. Release or convert "
                "the lease at the DHCP server; the IPAM row will be removed "
                "automatically on the next pull cycle."
            ),
        )

    subnet = await db.get(Subnet, ip.subnet_id)

    # Clean up any DHCP static reservations tied to this IP. The FK is
    # ``ondelete=SET NULL`` (so a stray orphan row can't block an IP delete),
    # but without this step a reservation stays on Windows / Kea forever and
    # the DB row lingers with a null ip_address_id. Push the delete through
    # the write-through first — if Windows refuses, we raise 502 before
    # committing and no drift is introduced.
    statics_res = await db.execute(
        select(DHCPStaticAssignment).where(DHCPStaticAssignment.ip_address_id == address_id)
    )
    statics_rows = list(statics_res.scalars().all())
    # Batched push on windows_dhcp servers (one WinRM round trip per
    # server instead of one per row); ABC default loops sequentially for
    # Kea / ISC. Typically one row — the batch overhead is negligible.
    await push_statics_bulk_delete(db, statics_rows)
    for static in statics_rows:
        await db.delete(static)

    if permanent:
        subnet_id = ip.subnet_id
        # Remove auto-generated DNS record before deleting the IP (FK would null it anyway,
        # but we want a clean delete and fqdn cleared)
        if subnet:
            await _sync_dns_record(db, ip, subnet, action="delete")
        db.add(
            _audit(
                current_user,
                "delete",
                "ip_address",
                str(ip.id),
                str(ip.address),
                old_value={"address": str(ip.address), "status": ip.status},
            )
        )
        await db.delete(ip)
        await db.flush()
        await _update_utilization(db, subnet_id)
        if subnet:
            await _update_block_utilization(db, subnet.block_id)
    else:
        # Soft-delete: mark as orphan; remove DNS record since the name is being released
        if subnet:
            await _sync_dns_record(db, ip, subnet, action="delete")
        old_status = ip.status
        ip.status = "orphan"
        db.add(
            _audit(
                current_user,
                "update",
                "ip_address",
                str(ip.id),
                str(ip.address),
                old_value={"status": old_status},
                new_value={"status": "orphan"},
            )
        )
        await db.flush()
        await _update_utilization(db, ip.subnet_id)
        if subnet:
            await _update_block_utilization(db, subnet.block_id)
    await db.commit()


class PurgeOrphansRequest(BaseModel):
    ip_ids: list[uuid.UUID]


@router.post("/subnets/{subnet_id}/orphans/purge")
async def purge_orphans(
    subnet_id: uuid.UUID,
    body: PurgeOrphansRequest,
    current_user: CurrentUser,
    db: DB,
) -> dict[str, int]:
    """Permanently delete the given orphan IPs in this subnet.

    The UI lists orphans from `GET /subnets/{id}/addresses?status=orphan` (client-
    side filter) and passes the chosen ids here. We scope by subnet so a stale UI
    can't purge rows from a different subnet.
    """
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")
    if not body.ip_ids:
        return {"purged": 0}
    res = await db.execute(
        select(IPAddress).where(
            IPAddress.subnet_id == subnet_id,
            IPAddress.id.in_(body.ip_ids),
            IPAddress.status == "orphan",
        )
    )
    rows = list(res.scalars().all())
    # Gather any lingering DHCP reservations across all orphan IPs in one
    # query, then push-delete them in one batched WinRM round trip per
    # server (versus one round trip per IP × one per static). Normally
    # ``delete_address``'s orphan-transition already removed these; the
    # loop catches older orphans created before that code existed.
    stat_res = await db.execute(
        select(DHCPStaticAssignment).where(
            DHCPStaticAssignment.ip_address_id.in_([ip.id for ip in rows])
        )
    )
    lingering_statics = list(stat_res.scalars().all())
    await push_statics_bulk_delete(db, lingering_statics)
    for static in lingering_statics:
        await db.delete(static)

    for ip in rows:
        # Best-effort DNS teardown (the FK would null it, but be explicit).
        try:
            await _sync_dns_record(db, ip, subnet, action="delete")
        except Exception:  # noqa: BLE001
            pass
        db.add(
            _audit(
                current_user,
                "delete",
                "ip_address",
                str(ip.id),
                str(ip.address),
                old_value={"address": str(ip.address), "status": "orphan"},
            )
        )
        await db.delete(ip)
    await db.flush()
    await _update_utilization(db, subnet_id)
    await _update_block_utilization(db, subnet.block_id)
    await db.commit()
    return {"purged": len(rows)}


# ── Next available IP ──────────────────────────────────────────────────────────


@router.post(
    "/subnets/{subnet_id}/next",
    response_model=IPAddressResponse,
    status_code=status.HTTP_201_CREATED,
)
async def allocate_next_ip(
    subnet_id: uuid.UUID, body: NextIPRequest, current_user: CurrentUser, db: DB
) -> IPAddress:
    """Atomically allocate the next available IP in the subnet."""
    # Lock the subnet row to serialise concurrent allocations. `of=Subnet`
    # restricts the FOR UPDATE to the base table; Subnet has joined-eager
    # relationships (vlan, etc.) and Postgres rejects FOR UPDATE on the
    # nullable side of an outer join.
    result = await db.execute(
        select(Subnet).where(Subnet.id == subnet_id).with_for_update(of=Subnet)
    )
    subnet = result.unique().scalar_one_or_none()
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    if body.status == "static_dhcp" and not body.mac_address:
        raise HTTPException(
            status_code=422,
            detail="mac_address is required when status is 'static_dhcp'",
        )

    explicit_zone = uuid.UUID(body.dns_zone_id) if body.dns_zone_id else None
    effective_zone = explicit_zone or await _resolve_effective_zone(db, subnet)
    if not body.force:
        warnings = await _check_ip_collisions(
            db,
            hostname=body.hostname,
            forward_zone_id=effective_zone,
            mac_address=body.mac_address,
        )
        if warnings:
            raise _collision_http_exc(warnings)

    net = _parse_network(str(subnet.network))

    # Fetch all used addresses in this subnet
    used_result = await db.execute(
        select(IPAddress.address).where(IPAddress.subnet_id == subnet_id)
    )
    # Normalise to string set; asyncpg returns INET as str
    used: set[str] = {str(row[0]) for row in used_result}

    # IPv6 subnets are typically /64 or larger — enumerating 2^64 addresses
    # is impossible, and even "random" would need a hash-based scheme with
    # duplicate checks. Rather than misbehave, surface a clear 409 and make
    # the UI side fall back to manual allocation (per the IPv6 roadmap item).
    if isinstance(net, ipaddress.IPv6Network):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "IPv6 subnets do not support auto-allocation. "
                "Please specify an explicit address."
            ),
        )

    # For large IPv4 subnets, cap the linear search at 65k hosts.
    max_search = 65536
    hosts = list(net.hosts()) if net.prefixlen >= 16 else list(net.hosts())[:max_search]

    if body.strategy == "random":
        import random

        random.shuffle(hosts)

    chosen: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    for host in hosts:
        if str(host) not in used:
            chosen = host
            break

    if chosen is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No available IP addresses in this subnet",
        )

    ip = IPAddress(
        subnet_id=subnet_id,
        address=str(chosen),
        status=body.status,
        hostname=body.hostname,
        mac_address=body.mac_address,
        description=body.description,
        custom_fields=body.custom_fields,
        tags=body.tags,
        created_by_user_id=current_user.id,
    )
    db.add(ip)
    await db.flush()

    # Sync DNS A record
    await _sync_dns_record(db, ip, subnet, zone_id=explicit_zone, action="create")
    # User-specified alias records (CNAME / A) tied to this IP
    if body.aliases:
        await _create_alias_records(db, ip, subnet, body.aliases, zone_id=explicit_zone)

    db.add(
        _audit(
            current_user,
            "create",
            "ip_address",
            str(ip.id),
            str(chosen),
            new_value={
                **body.model_dump(mode="json", exclude={"dns_zone_id", "force"}),
                "address": str(chosen),
            },
        )
    )
    await db.flush()
    await _update_utilization(db, subnet_id)
    await _update_block_utilization(db, subnet.block_id)
    await db.commit()
    await db.refresh(ip)
    logger.info(
        "ip_allocated",
        ip_id=str(ip.id),
        address=str(chosen),
        subnet_id=str(subnet_id),
        strategy=body.strategy,
    )
    return ip


# ── Subnet ↔ DNS Domain associations (§11) ────────────────────────────────────


class SubnetDomainCreate(BaseModel):
    dns_zone_id: uuid.UUID
    is_primary: bool = False


class SubnetDomainResponse(BaseModel):
    id: uuid.UUID
    subnet_id: uuid.UUID
    dns_zone_id: uuid.UUID
    is_primary: bool
    zone_name: str | None = None

    model_config = {"from_attributes": True}


async def _sync_primary_zone_pointer(db: AsyncSession, subnet_id: uuid.UUID) -> None:
    """Keep `subnet.dns_zone_id` pointing at the row flagged `is_primary` (if any).

    If no row is primary, sets the pointer to NULL.  The column exists on
    `subnet` as a text convenience pointer (see migration a1b2c3d4e5f6); we
    write it via raw SQL because it is not declared on the ORM model.
    """
    result = await db.execute(
        select(SubnetDomain.dns_zone_id)
        .where(SubnetDomain.subnet_id == subnet_id)
        .where(SubnetDomain.is_primary.is_(True))
        .limit(1)
    )
    primary = result.scalar_one_or_none()
    await db.execute(
        text("UPDATE subnet SET dns_zone_id = :zid WHERE id = CAST(:sid AS uuid)"),
        {"zid": str(primary) if primary else None, "sid": str(subnet_id)},
    )


@router.get("/subnets/{subnet_id}/domains", response_model=list[SubnetDomainResponse])
async def list_subnet_domains(
    subnet_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> list[SubnetDomainResponse]:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    result = await db.execute(
        select(SubnetDomain, DNSZone)
        .join(DNSZone, SubnetDomain.dns_zone_id == DNSZone.id)
        .where(SubnetDomain.subnet_id == subnet_id)
        .order_by(SubnetDomain.is_primary.desc(), DNSZone.name)
    )
    out: list[SubnetDomainResponse] = []
    for sd, zone in result.all():
        out.append(
            SubnetDomainResponse(
                id=sd.id,
                subnet_id=sd.subnet_id,
                dns_zone_id=sd.dns_zone_id,
                is_primary=sd.is_primary,
                zone_name=zone.name,
            )
        )
    return out


@router.post(
    "/subnets/{subnet_id}/domains",
    response_model=SubnetDomainResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_subnet_domain(
    subnet_id: uuid.UUID,
    body: SubnetDomainCreate,
    current_user: CurrentUser,
    db: DB,
) -> SubnetDomainResponse:
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    zone = await db.get(DNSZone, body.dns_zone_id)
    if zone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DNS zone not found")

    existing = await db.execute(
        select(SubnetDomain).where(
            SubnetDomain.subnet_id == subnet_id,
            SubnetDomain.dns_zone_id == body.dns_zone_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This DNS zone is already associated with this subnet",
        )

    # Only one primary per subnet: demote others if this one is primary.
    if body.is_primary:
        await db.execute(
            text(
                "UPDATE subnet_domain SET is_primary = false "
                "WHERE subnet_id = CAST(:sid AS uuid)"
            ),
            {"sid": str(subnet_id)},
        )

    sd = SubnetDomain(
        subnet_id=subnet_id,
        dns_zone_id=body.dns_zone_id,
        is_primary=body.is_primary,
    )
    db.add(sd)
    await db.flush()

    await _sync_primary_zone_pointer(db, subnet_id)

    db.add(
        _audit(
            current_user,
            "create",
            "subnet_domain",
            str(sd.id),
            f"{subnet.network} → {zone.name}",
            new_value={
                "subnet_id": str(subnet_id),
                "dns_zone_id": str(body.dns_zone_id),
                "is_primary": body.is_primary,
            },
        )
    )
    await db.commit()
    await db.refresh(sd)
    return SubnetDomainResponse(
        id=sd.id,
        subnet_id=sd.subnet_id,
        dns_zone_id=sd.dns_zone_id,
        is_primary=sd.is_primary,
        zone_name=zone.name,
    )


@router.delete(
    "/subnets/{subnet_id}/domains/{domain_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_subnet_domain(
    subnet_id: uuid.UUID,
    domain_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
) -> None:
    sd = await db.get(SubnetDomain, domain_id)
    if sd is None or sd.subnet_id != subnet_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet domain not found")

    db.add(
        _audit(
            current_user,
            "delete",
            "subnet_domain",
            str(sd.id),
            f"subnet={subnet_id} zone={sd.dns_zone_id}",
            old_value={
                "subnet_id": str(sd.subnet_id),
                "dns_zone_id": str(sd.dns_zone_id),
                "is_primary": sd.is_primary,
            },
        )
    )
    await db.delete(sd)
    await db.flush()
    await _sync_primary_zone_pointer(db, subnet_id)
    await db.commit()


# ── Subnet bulk edit (§11) ────────────────────────────────────────────────────


_BULK_ALLOWED_STATUSES = {"active", "deprecated", "reserved", "quarantine"}


class SubnetBulkChanges(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    vlan_id: int | None = None
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in _BULK_ALLOWED_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(_BULK_ALLOWED_STATUSES))}")
        return v


class SubnetBulkEditRequest(BaseModel):
    subnet_ids: list[uuid.UUID]
    changes: SubnetBulkChanges


class SubnetBulkEditResponse(BaseModel):
    batch_id: uuid.UUID
    updated_count: int
    not_found: list[uuid.UUID] = []


@router.post("/subnets/bulk-edit", response_model=SubnetBulkEditResponse)
async def bulk_edit_subnets(
    body: SubnetBulkEditRequest,
    current_user: CurrentUser,
    db: DB,
) -> SubnetBulkEditResponse:
    """Apply the same set of changes to multiple subnets atomically.

    All mutations happen in a single transaction; one audit row per
    successfully-updated subnet shares a `batch_id` in `new_value`.
    """
    changes = body.changes.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided in changes",
        )
    if not body.subnet_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="subnet_ids must not be empty",
        )

    batch_id = uuid.uuid4()
    updated = 0
    not_found: list[uuid.UUID] = []

    for sid in body.subnet_ids:
        subnet = await db.get(Subnet, sid)
        if subnet is None:
            not_found.append(sid)
            continue

        old = {k: getattr(subnet, k, None) for k in changes.keys()}
        for field, value in changes.items():
            setattr(subnet, field, value)

        db.add(
            _audit(
                current_user,
                "update",
                "subnet",
                str(subnet.id),
                f"{subnet.network} ({subnet.name})",
                old_value={**{k: (str(v) if v is not None else None) for k, v in old.items()}},
                new_value={**changes, "batch_id": str(batch_id)},
            )
        )
        updated += 1

    await db.commit()
    logger.info(
        "subnet_bulk_edit",
        user=current_user.username,
        batch_id=str(batch_id),
        requested=len(body.subnet_ids),
        updated=updated,
        not_found=len(not_found),
    )
    return SubnetBulkEditResponse(batch_id=batch_id, updated_count=updated, not_found=not_found)


# ── Effective fields (tags + custom_fields inheritance, §11) ──────────────────


class EffectiveFieldsResponse(BaseModel):
    subnet_id: uuid.UUID
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    # Per-key source trail: "subnet" | "block:<id>" | "space:<id>"
    tag_sources: dict[str, str]
    custom_field_sources: dict[str, str]


@router.get(
    "/subnets/{subnet_id}/effective-fields",
    response_model=EffectiveFieldsResponse,
)
async def get_effective_fields(
    subnet_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> EffectiveFieldsResponse:
    """Merge tags + custom_fields up the Subnet → Block(s) → Space chain.

    Closer-to-leaf values override farther-from-leaf; storage is NOT modified.
    """
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    # Collect the chain, leaf-last: [space, block_root, ..., block_leaf, subnet]
    chain: list[tuple[str, str, dict, dict]] = []

    space = await db.get(IPSpace, subnet.space_id)
    if space is not None:
        # IPSpace has tags but no custom_fields column.
        chain.append((f"space:{space.id}", "space", space.tags or {}, {}))

    # Walk block ancestors (root → leaf).
    block_path: list[IPBlock] = []
    cur: IPBlock | None = await db.get(IPBlock, subnet.block_id)
    while cur is not None:
        block_path.append(cur)
        if cur.parent_block_id is None:
            break
        cur = await db.get(IPBlock, cur.parent_block_id)
    for b in reversed(block_path):
        chain.append((f"block:{b.id}", "block", b.tags or {}, b.custom_fields or {}))

    chain.append(("subnet", "subnet", subnet.tags or {}, subnet.custom_fields or {}))

    tags: dict[str, Any] = {}
    custom_fields: dict[str, Any] = {}
    tag_sources: dict[str, str] = {}
    custom_field_sources: dict[str, str] = {}

    for src, _kind, tmap, cmap in chain:
        for k, v in tmap.items():
            tags[k] = v
            tag_sources[k] = src
        for k, v in cmap.items():
            custom_fields[k] = v
            custom_field_sources[k] = src

    return EffectiveFieldsResponse(
        subnet_id=subnet.id,
        tags=tags,
        custom_fields=custom_fields,
        tag_sources=tag_sources,
        custom_field_sources=custom_field_sources,
    )


class BlockEffectiveFieldsResponse(BaseModel):
    block_id: uuid.UUID
    tags: dict[str, Any]
    custom_fields: dict[str, Any]
    # Per-key source trail: "block:<id>" | "space:<id>"
    tag_sources: dict[str, str]
    custom_field_sources: dict[str, str]


@router.get(
    "/blocks/{block_id}/effective-fields",
    response_model=BlockEffectiveFieldsResponse,
)
async def get_block_effective_fields(
    block_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> BlockEffectiveFieldsResponse:
    """Merge tags + custom_fields up the Block → parent Block(s) → Space chain.

    Closer-to-leaf values override farther-from-leaf; storage is NOT modified.
    The ``sources`` maps let callers tell which key came from which ancestor,
    so the edit modal can render inherited values as placeholders with a
    "inherited from <ancestor>" badge.
    """
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")

    # Collect the chain, leaf-last: [space, root_block, ..., this_block]
    chain: list[tuple[str, dict, dict]] = []

    space = await db.get(IPSpace, block.space_id)
    if space is not None:
        chain.append((f"space:{space.id}", space.tags or {}, {}))

    block_path: list[IPBlock] = []
    cur: IPBlock | None = block
    while cur is not None:
        block_path.append(cur)
        if cur.parent_block_id is None:
            break
        cur = await db.get(IPBlock, cur.parent_block_id)
    for b in reversed(block_path):
        chain.append((f"block:{b.id}", b.tags or {}, b.custom_fields or {}))

    tags: dict[str, Any] = {}
    custom_fields: dict[str, Any] = {}
    tag_sources: dict[str, str] = {}
    custom_field_sources: dict[str, str] = {}
    for src, tmap, cmap in chain:
        for k, v in tmap.items():
            tags[k] = v
            tag_sources[k] = src
        for k, v in cmap.items():
            custom_fields[k] = v
            custom_field_sources[k] = src

    return BlockEffectiveFieldsResponse(
        block_id=block.id,
        tags=tags,
        custom_fields=custom_fields,
        tag_sources=tag_sources,
        custom_field_sources=custom_field_sources,
    )


# ── Subnet aliases (aggregate CNAME/A records for every IP in the subnet) ─────


class SubnetAliasResponse(BaseModel):
    id: uuid.UUID
    zone_id: uuid.UUID
    name: str
    record_type: str
    value: str
    fqdn: str
    ip_address_id: uuid.UUID
    ip_address: str
    ip_hostname: str | None

    model_config = {"from_attributes": True}


@router.get("/subnets/{subnet_id}/aliases", response_model=list[SubnetAliasResponse])
async def list_subnet_aliases(
    subnet_id: uuid.UUID,
    current_user: CurrentUser,
    db: DB,
) -> list[SubnetAliasResponse]:
    """List every user-added DNS alias (CNAME or secondary A) for IPs in this subnet.

    Primary A records (pointed to by ``IPAddress.dns_record_id``) are excluded.
    """
    if await db.get(Subnet, subnet_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subnet not found")

    ips = list(
        (await db.execute(select(IPAddress).where(IPAddress.subnet_id == subnet_id)))
        .scalars()
        .all()
    )
    if not ips:
        return []
    ip_by_id = {ip.id: ip for ip in ips}
    primary_ids = {ip.dns_record_id for ip in ips if ip.dns_record_id is not None}
    conds = [
        DNSRecord.ip_address_id.in_(list(ip_by_id.keys())),
        DNSRecord.auto_generated.is_(True),
        DNSRecord.record_type.in_(["CNAME", "A"]),
    ]
    if primary_ids:
        conds.append(DNSRecord.id.notin_(primary_ids))
    records = list((await db.execute(select(DNSRecord).where(*conds))).scalars().all())
    out: list[SubnetAliasResponse] = []
    for rec in records:
        ip = ip_by_id.get(rec.ip_address_id) if rec.ip_address_id else None
        if ip is None:
            continue
        out.append(
            SubnetAliasResponse(
                id=rec.id,
                zone_id=rec.zone_id,
                name=rec.name,
                record_type=rec.record_type,
                value=rec.value,
                fqdn=rec.fqdn,
                ip_address_id=ip.id,
                ip_address=str(ip.address),
                ip_hostname=ip.hostname,
            )
        )
    out.sort(key=lambda a: (a.ip_address, a.record_type, a.name))
    return out


# ── IP address bulk delete / bulk edit ────────────────────────────────────────


class IPAddressBulkDeleteRequest(BaseModel):
    ip_ids: list[uuid.UUID]
    permanent: bool = False


class IPAddressBulkDeleteResponse(BaseModel):
    deleted_count: int
    not_found: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []  # system rows (network/broadcast) or already-orphaned


@router.post("/addresses/bulk-delete", response_model=IPAddressBulkDeleteResponse)
async def bulk_delete_addresses(
    body: IPAddressBulkDeleteRequest,
    current_user: CurrentUser,
    db: DB,
) -> IPAddressBulkDeleteResponse:
    """Soft-delete (→ orphan) or permanently delete multiple IPs in one call.

    System rows (``network``/``broadcast``) are always skipped. When
    ``permanent=False``, rows already in ``orphan`` are skipped; when
    ``permanent=True``, any row is purged.
    """
    if not body.ip_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ip_ids must not be empty",
        )
    batch_id = uuid.uuid4()
    rows = list(
        (await db.execute(select(IPAddress).where(IPAddress.id.in_(body.ip_ids)))).scalars().all()
    )
    found_ids = {ip.id for ip in rows}
    not_found = [i for i in body.ip_ids if i not in found_ids]
    deleted = 0
    skipped: list[uuid.UUID] = []
    subnets_touched: set[uuid.UUID] = set()

    for ip in rows:
        if ip.status in ("network", "broadcast"):
            skipped.append(ip.id)
            continue
        if not body.permanent and ip.status == "orphan":
            skipped.append(ip.id)
            continue
        subnet = await db.get(Subnet, ip.subnet_id)
        if subnet is not None:
            try:
                await _sync_dns_record(db, ip, subnet, action="delete")
            except Exception:  # noqa: BLE001
                pass
        if body.permanent:
            db.add(
                _audit(
                    current_user,
                    "delete",
                    "ip_address",
                    str(ip.id),
                    str(ip.address),
                    old_value={"address": str(ip.address), "status": ip.status},
                    new_value={"batch_id": str(batch_id)},
                )
            )
            await db.delete(ip)
        else:
            old_status = ip.status
            ip.status = "orphan"
            db.add(
                _audit(
                    current_user,
                    "update",
                    "ip_address",
                    str(ip.id),
                    str(ip.address),
                    old_value={"status": old_status},
                    new_value={"status": "orphan", "batch_id": str(batch_id)},
                )
            )
        subnets_touched.add(ip.subnet_id)
        deleted += 1

    await db.flush()
    for sid in subnets_touched:
        await _update_utilization(db, sid)
        s = await db.get(Subnet, sid)
        if s is not None:
            await _update_block_utilization(db, s.block_id)
    await db.commit()
    logger.info(
        "ip_address_bulk_delete",
        user=current_user.username,
        batch_id=str(batch_id),
        requested=len(body.ip_ids),
        deleted=deleted,
        permanent=body.permanent,
    )
    return IPAddressBulkDeleteResponse(deleted_count=deleted, not_found=not_found, skipped=skipped)


_IP_BULK_ALLOWED_STATUSES = {
    "available",
    "allocated",
    "reserved",
    "static_dhcp",
    "deprecated",
}


class IPAddressBulkChanges(BaseModel):
    status: str | None = None
    description: str | None = None
    # Merged into existing dicts (set key=null to remove).
    tags: dict[str, Any] | None = None
    custom_fields: dict[str, Any] | None = None
    # Apply a forward DNS zone change to every selected IP. Triggers per-row
    # record reconciliation (create / move / delete) via _sync_dns_record.
    # Use the empty string ``""`` to clear a zone assignment.
    dns_zone_id: str | None = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in _IP_BULK_ALLOWED_STATUSES:
            raise ValueError(
                f"status must be one of: {', '.join(sorted(_IP_BULK_ALLOWED_STATUSES))}"
            )
        return v


class IPAddressBulkEditRequest(BaseModel):
    ip_ids: list[uuid.UUID]
    changes: IPAddressBulkChanges


class IPAddressBulkEditResponse(BaseModel):
    batch_id: uuid.UUID
    updated_count: int
    not_found: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []


@router.post("/addresses/bulk-edit", response_model=IPAddressBulkEditResponse)
async def bulk_edit_addresses(
    body: IPAddressBulkEditRequest,
    current_user: CurrentUser,
    db: DB,
) -> IPAddressBulkEditResponse:
    """Apply the same change set to multiple IPs.

    * ``status`` and ``description`` replace the existing value.
    * ``tags`` and ``custom_fields`` are **merged** into the existing dicts —
      set a key to ``null`` to remove it. This matches UX expectations when
      bulk-tagging a set of IPs.
    * System rows (``network``/``broadcast``/``orphan``) are skipped.
    """
    changes = body.changes.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided in changes",
        )
    if not body.ip_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ip_ids must not be empty",
        )

    batch_id = uuid.uuid4()
    rows = list(
        (await db.execute(select(IPAddress).where(IPAddress.id.in_(body.ip_ids)))).scalars().all()
    )
    found_ids = {ip.id for ip in rows}
    not_found = [i for i in body.ip_ids if i not in found_ids]
    updated = 0
    skipped: list[uuid.UUID] = []

    tags_patch = body.changes.tags
    cf_patch = body.changes.custom_fields
    scalar_changes = {k: v for k, v in changes.items() if k in ("status", "description")}
    apply_zone = body.changes.dns_zone_id is not None
    new_zone_id: uuid.UUID | None = None
    if apply_zone and body.changes.dns_zone_id:
        try:
            new_zone_id = uuid.UUID(body.changes.dns_zone_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dns_zone_id must be a valid UUID or the empty string to clear",
            ) from None

    # Cache the subnet rows so per-IP DNS sync doesn't re-query for every row.
    subnet_cache: dict[uuid.UUID, Subnet] = {}

    for ip in rows:
        if ip.status in ("network", "broadcast", "orphan"):
            skipped.append(ip.id)
            continue
        # Skip dynamic-lease mirrors — DHCP server owns their state and any
        # edit here would be overwritten on the next pull. The UI already
        # blocks these from being selected; this is defence-in-depth.
        if ip.auto_from_lease:
            skipped.append(ip.id)
            continue

        old: dict[str, Any] = {}
        for k in scalar_changes:
            old[k] = getattr(ip, k, None)
        for k, v in scalar_changes.items():
            setattr(ip, k, v)

        if tags_patch is not None:
            merged = dict(ip.tags or {})
            for k, v in tags_patch.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            old["tags"] = ip.tags or {}
            ip.tags = merged
        if cf_patch is not None:
            merged_cf = dict(ip.custom_fields or {})
            for k, v in cf_patch.items():
                if v is None:
                    merged_cf.pop(k, None)
                else:
                    merged_cf[k] = v
            old["custom_fields"] = ip.custom_fields or {}
            ip.custom_fields = merged_cf

        if apply_zone:
            subnet = subnet_cache.get(ip.subnet_id)
            if subnet is None:
                subnet = await db.get(Subnet, ip.subnet_id)
                if subnet is not None:
                    subnet_cache[ip.subnet_id] = subnet
            if subnet is not None:
                old["forward_zone_id"] = str(ip.forward_zone_id) if ip.forward_zone_id else None
                await _sync_dns_record(db, ip, subnet, zone_id=new_zone_id, action="update")

        db.add(
            _audit(
                current_user,
                "update",
                "ip_address",
                str(ip.id),
                str(ip.address),
                old_value={k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in old.items()},
                new_value={**changes, "batch_id": str(batch_id)},
            )
        )
        updated += 1

    await db.commit()
    logger.info(
        "ip_address_bulk_edit",
        user=current_user.username,
        batch_id=str(batch_id),
        requested=len(body.ip_ids),
        updated=updated,
    )
    return IPAddressBulkEditResponse(
        batch_id=batch_id,
        updated_count=updated,
        not_found=not_found,
        skipped=skipped,
    )
