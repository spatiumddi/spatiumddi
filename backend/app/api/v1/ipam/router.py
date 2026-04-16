"""IPAM API — IP spaces, blocks, subnets, and addresses."""

import ipaddress
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentUser
from app.api.v1.ipam.io_router import router as io_router
from app.models.audit import AuditLog
from app.models.dns import DNSRecord, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet, SubnetDomain
from app.models.vlans import VLAN

logger = structlog.get_logger(__name__)
router = APIRouter()
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


def _total_ips(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> int:
    """Usable host count (excludes network/broadcast for prefixlen < 31)."""
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


async def _resolve_effective_zone(db: AsyncSession, subnet: Subnet) -> uuid.UUID | None:
    """Return the effective DNS zone UUID for a subnet, walking the block ancestor chain."""
    if not subnet.dns_inherit_settings and subnet.dns_zone_id:
        return uuid.UUID(subnet.dns_zone_id)
    block_id = subnet.block_id
    while block_id:
        block = await db.get(IPBlock, block_id)
        if block is None:
            break
        if not block.dns_inherit_settings and block.dns_zone_id:
            return uuid.UUID(block.dns_zone_id)
        block_id = block.parent_block_id
    return None


async def _resolve_reverse_zone(
    db: AsyncSession, subnet: Subnet, ip_addr: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> DNSZone | None:
    """Find the reverse zone covering this IP. Prefers a zone linked to the
    subnet; falls back to any reverse zone in the subnet's DNS group whose
    name is a suffix of the IP's reverse_pointer."""
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
    # 2. Walk effective DNS group(s) for the subnet
    group_ids = subnet.dns_group_ids or []
    if not group_ids:
        return None
    res = await db.execute(
        select(DNSZone).where(
            DNSZone.group_id.in_(group_ids),
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
    for al in aliases:
        rtype = (getattr(al, "record_type", None) or al.get("record_type") or "CNAME").upper()
        name = (getattr(al, "name", None) or al.get("name") or "").strip().rstrip(".")
        if not name or rtype not in {"CNAME", "A"}:
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

    zone_domain = zone.name.rstrip(".")
    fqdn = f"{ip.hostname}.{zone_domain}"
    ip.fqdn = fqdn

    # ── Forward A ───────────────────────────────────────────────────────────
    # Skip forward DNS for the default gateway placeholder hostname.
    # Every subnet has one, so syncing them all would create N copies of
    # `gateway.example.com` that resolve to different IPs — useless and noisy.
    # When a user renames the gateway IP to something specific (e.g.
    # "core-rtr1"), normal A-record sync resumes. Reverse PTR is still
    # created below since reverse lookups for the gateway IP are useful.
    is_default_gateway_name = ip.hostname == "gateway"

    result = await db.execute(
        select(DNSRecord).where(
            DNSRecord.ip_address_id == ip.id,
            DNSRecord.auto_generated.is_(True),
            DNSRecord.record_type == "A",
        )
    )
    existing_a = result.scalars().all()

    if is_default_gateway_name:
        # Tear down any A record that may have been published before the user
        # renamed the IP back to the default. PTR continues below.
        for record in existing_a:
            old_zone = await db.get(DNSZone, record.zone_id)
            if old_zone is not None:
                await _enqueue_dns_op(
                    db,
                    old_zone,
                    "delete",
                    record.name,
                    "A",
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
            record_type="A",
            value=str(ip.address),
            auto_generated=True,
            ip_address_id=ip.id,
            created_by_user_id=ip.created_by_user_id,
        )
        db.add(a_rec)
        await db.flush()
        ip.dns_record_id = a_rec.id
        ip.forward_zone_id = effective_zone_id
        await _enqueue_dns_op(db, zone, "create", ip.hostname, "A", str(ip.address), None)
    else:
        for record in existing_a:
            if record.zone_id != effective_zone_id:
                old_zone = await db.get(DNSZone, record.zone_id)
                if old_zone is not None:
                    await _enqueue_dns_op(
                        db, old_zone, "delete", record.name, "A", record.value, record.ttl
                    )
                await db.delete(record)
                new_a = DNSRecord(
                    zone_id=effective_zone_id,
                    name=ip.hostname,
                    fqdn=fqdn,
                    record_type="A",
                    value=str(ip.address),
                    auto_generated=True,
                    ip_address_id=ip.id,
                    created_by_user_id=ip.created_by_user_id,
                )
                db.add(new_a)
                await db.flush()
                ip.dns_record_id = new_a.id
                ip.forward_zone_id = effective_zone_id
                await _enqueue_dns_op(db, zone, "create", ip.hostname, "A", str(ip.address), None)
            else:
                changed = record.name != ip.hostname or record.value != str(ip.address)
                record.name = ip.hostname
                record.fqdn = fqdn
                record.value = str(ip.address)
                if changed:
                    await _enqueue_dns_op(
                        db, zone, "update", ip.hostname, "A", str(ip.address), record.ttl
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


class IPSpaceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_default: bool | None = None
    tags: dict[str, Any] | None = None
    dns_group_ids: list[str] | None = None
    dns_zone_id: str | None = None
    dns_additional_zone_ids: list[str] | None = None


class IPSpaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    is_default: bool
    tags: dict[str, Any]
    dns_group_ids: list[str] = []
    dns_zone_id: str | None = None
    dns_additional_zone_ids: list[str] = []
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

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"active", "deprecated", "reserved", "quarantine"}
        if v not in allowed:
            raise ValueError(f"status must be one of: {', '.join(sorted(allowed))}")
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
    block_id: uuid.UUID
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
    changes = body.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(space, field, value)

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


@router.delete("/spaces/{space_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_space(space_id: uuid.UUID, current_user: CurrentUser, db: DB) -> None:
    space = await db.get(IPSpace, space_id)
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="IP space not found")

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
        block.parent_block_id = new_parent_id

    changes = body.model_dump(
        exclude_none=True,
        exclude={
            "dns_group_ids",
            "dns_zone_id",
            "dns_additional_zone_ids",
            "dns_inherit_settings",
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
    prefix_len: int = Query(..., ge=1, le=32, description="Desired prefix length (IPv4 only)"),
    limit: int = Query(20, ge=1, le=50),
    current_user: CurrentUser = ...,  # type: ignore[assignment]
    db: DB = ...,  # type: ignore[assignment]
) -> list[str]:
    """Return available /prefix_len subnets within this block, sorted sequentially."""
    block = await db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block not found")

    block_net = ipaddress.ip_network(str(block.network), strict=False)
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
    auto_created: list[str] = []
    if net.prefixlen < 31 and not body.skip_auto_addresses:
        # Network address (e.g. 10.0.1.0)
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

        # Broadcast address (e.g. 10.0.1.255)
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
    }
    changes = body.model_dump(exclude_none=True, exclude=exclude_fields)
    changes_for_audit = body.model_dump(mode="json", exclude_none=True, exclude=exclude_fields)
    for field, value in changes.items():
        setattr(subnet, field, value)
    # Handle DNS fields explicitly so boolean False and explicit null are preserved
    dns_fields = {"dns_group_ids", "dns_zone_id", "dns_additional_zone_ids", "dns_inherit_settings"}
    for field in dns_fields & body.model_fields_set:
        setattr(subnet, field, getattr(body, field))

    # Handle add/remove of auto-created network/broadcast/gateway records
    if body.manage_auto_addresses is not None:
        net = _parse_network(str(subnet.network))
        if net.prefixlen < 31:
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
                if str(net.broadcast_address) not in existing_addrs:
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
    await db.commit()


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
        stale_res = await db.execute(
            select(DNSRecord)
            .where(
                DNSRecord.id.in_(body.delete_stale_record_ids),
                DNSRecord.auto_generated.is_(True),
            )
            .options(selectinload(DNSRecord.zone))
        )
        for rec in stale_res.scalars().all():
            try:
                if rec.zone is not None:
                    await _enqueue_dns_op(
                        db,
                        rec.zone,
                        "delete",
                        rec.name,
                        rec.record_type,
                        rec.value,
                        rec.ttl,
                    )
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


# ── IP Addresses ───────────────────────────────────────────────────────────────


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
    result = await db.execute(query)
    return list(result.scalars().all())


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

    ip = IPAddress(
        subnet_id=subnet_id,
        created_by_user_id=current_user.id,
        **body.model_dump(exclude={"dns_zone_id"}),
    )
    db.add(ip)
    await db.flush()

    # Sync DNS A record
    zone_id = uuid.UUID(body.dns_zone_id) if body.dns_zone_id else None
    await _sync_dns_record(db, ip, subnet, zone_id=zone_id, action="create")
    # User-specified alias records (CNAME / A) tied to this IP
    if body.aliases:
        await _create_alias_records(db, ip, subnet, body.aliases, zone_id=zone_id)

    db.add(
        _audit(
            current_user,
            "create",
            "ip_address",
            str(ip.id),
            body.address,
            new_value=body.model_dump(mode="json", exclude={"dns_zone_id"}),
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
    changes = body.model_dump(exclude_none=True, exclude={"dns_zone_id"})
    changes_for_audit = body.model_dump(mode="json", exclude_none=True, exclude={"dns_zone_id"})
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
async def list_aliases(
    address_id: uuid.UUID, current_user: CurrentUser, db: DB
) -> list[DNSRecord]:
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
        await _enqueue_dns_op(
            db, zone, "delete", rec.name, rec.record_type, rec.value, rec.ttl
        )
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

    subnet = await db.get(Subnet, ip.subnet_id)
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

    net = _parse_network(str(subnet.network))

    # Fetch all used addresses in this subnet
    used_result = await db.execute(
        select(IPAddress.address).where(IPAddress.subnet_id == subnet_id)
    )
    # Normalise to string set; asyncpg returns INET as str
    used: set[str] = {str(row[0]) for row in used_result}

    # For large subnets, cap the search at first 65536 hosts
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
    zone_id = uuid.UUID(body.dns_zone_id) if body.dns_zone_id else None
    await _sync_dns_record(db, ip, subnet, zone_id=zone_id, action="create")
    # User-specified alias records (CNAME / A) tied to this IP
    if body.aliases:
        await _create_alias_records(db, ip, subnet, body.aliases, zone_id=zone_id)

    db.add(
        _audit(
            current_user,
            "create",
            "ip_address",
            str(ip.id),
            str(chosen),
            new_value={
                **body.model_dump(mode="json", exclude={"dns_zone_id"}),
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
