"""Global search — IPAM + DNS resources."""

from __future__ import annotations

import ipaddress
import re
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DB
from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── Query-type detection ───────────────────────────────────────────────────────


def _is_ip(q: str) -> bool:
    try:
        ipaddress.ip_address(q)
        return True
    except ValueError:
        return False


def _is_cidr(q: str) -> bool:
    if "/" not in q:
        return False
    try:
        ipaddress.ip_network(q, strict=False)
        return True
    except ValueError:
        return False


_MAC_PATTERNS = [
    re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$"),
    re.compile(r"^([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}$"),
    re.compile(r"^[0-9a-fA-F]{12}$"),
    re.compile(r"^([0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}$"),  # Cisco dotted
]


def _is_mac(q: str) -> bool:
    return any(p.match(q) for p in _MAC_PATTERNS)


def _normalize_mac(q: str) -> str:
    """Strip separators and lowercase, for partial ILIKE matching."""
    return re.sub(r"[:\-\.]", "", q).lower()


# ── Response schema ────────────────────────────────────────────────────────────


class SearchResult(BaseModel):
    """One hit from any resource type."""

    type: str               # "ip_address"|"subnet"|"block"|"space"|"dns_zone"|"dns_record"|"dns_group"
    id: str

    # Primary display
    display: str            # e.g. "10.0.0.42" or "example.com."
    name: str | None        # human name if set

    # Status / detail
    status: str | None      # ip or subnet status
    description: str | None

    # IP-address specific
    hostname: str | None
    mac_address: str | None

    # Breadcrumb context (IPAM)
    subnet_id: str | None
    subnet_network: str | None
    block_id: str | None
    space_id: str | None
    space_name: str | None

    # DNS context
    dns_group_id: str | None = None
    dns_group_name: str | None = None
    dns_zone_id: str | None = None
    dns_zone_name: str | None = None
    dns_record_type: str | None = None
    dns_record_value: str | None = None


class SearchResponse(BaseModel):
    query: str
    total: int
    results: list[SearchResult]


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _search_addresses(
    db: AsyncSession, q: str, limit: int
) -> list[SearchResult]:
    is_ip = _is_ip(q)
    is_mac = _is_mac(q)

    stmt = (
        select(IPAddress, Subnet, IPSpace)
        .join(Subnet, IPAddress.subnet_id == Subnet.id)
        .join(IPSpace, Subnet.space_id == IPSpace.id)
    )

    if is_ip:
        stmt = stmt.where(
            text("CAST(ip_address.address AS inet) = CAST(:q AS inet)")
        ).params(q=q)
    elif is_mac:
        norm = _normalize_mac(q)
        stmt = stmt.where(
            text("REPLACE(REPLACE(REPLACE(CAST(ip_address.mac_address AS text), ':', ''), '-', ''), '.', '') ILIKE :norm")
        ).params(norm=f"%{norm}%")
    else:
        stmt = stmt.where(
            or_(
                IPAddress.hostname.ilike(f"%{q}%"),
                IPAddress.description.ilike(f"%{q}%"),
                text("CAST(ip_address.mac_address AS text) ILIKE :q").params(q=f"%{q}%"),
            )
        )

    result = await db.execute(stmt.limit(limit))
    rows = result.all()

    out = []
    for ip, subnet, space in rows:
        out.append(
            SearchResult(
                type="ip_address",
                id=str(ip.id),
                display=str(ip.address),
                name=ip.hostname,
                status=ip.status,
                description=ip.description or None,
                hostname=ip.hostname,
                mac_address=str(ip.mac_address) if ip.mac_address else None,
                subnet_id=str(ip.subnet_id),
                subnet_network=str(subnet.network),
                block_id=str(subnet.block_id) if subnet.block_id else None,
                space_id=str(space.id),
                space_name=space.name,
            )
        )
    return out


async def _search_subnets(
    db: AsyncSession, q: str, limit: int
) -> list[SearchResult]:
    is_ip = _is_ip(q)
    is_cidr = _is_cidr(q)

    stmt = (
        select(Subnet, IPSpace)
        .join(IPSpace, Subnet.space_id == IPSpace.id)
    )

    if is_cidr:
        # Subnets that are within or equal to the query CIDR
        stmt = stmt.where(
            text("CAST(subnet.network AS cidr) <<= CAST(:q AS cidr)").params(q=q)
        )
    elif is_ip:
        # Subnets containing this IP
        stmt = stmt.where(
            text("CAST(subnet.network AS cidr) >> CAST(:q AS inet)").params(q=q)
        )
    else:
        stmt = stmt.where(
            or_(
                Subnet.name.ilike(f"%{q}%"),
                Subnet.description.ilike(f"%{q}%"),
            )
        )

    result = await db.execute(stmt.limit(limit))
    rows = result.all()

    out = []
    for subnet, space in rows:
        out.append(
            SearchResult(
                type="subnet",
                id=str(subnet.id),
                display=str(subnet.network),
                name=subnet.name or None,
                status=subnet.status,
                description=subnet.description or None,
                hostname=None,
                mac_address=None,
                subnet_id=str(subnet.id),
                subnet_network=str(subnet.network),
                block_id=str(subnet.block_id) if subnet.block_id else None,
                space_id=str(space.id),
                space_name=space.name,
            )
        )
    return out


async def _search_blocks(
    db: AsyncSession, q: str, limit: int
) -> list[SearchResult]:
    is_cidr = _is_cidr(q)
    is_ip = _is_ip(q)

    stmt = (
        select(IPBlock, IPSpace)
        .join(IPSpace, IPBlock.space_id == IPSpace.id)
    )

    if is_cidr:
        stmt = stmt.where(
            text("CAST(ip_block.network AS cidr) <<= CAST(:q AS cidr)").params(q=q)
        )
    elif is_ip:
        stmt = stmt.where(
            text("CAST(ip_block.network AS cidr) >> CAST(:q AS inet)").params(q=q)
        )
    else:
        stmt = stmt.where(
            or_(
                IPBlock.name.ilike(f"%{q}%"),
                IPBlock.description.ilike(f"%{q}%"),
            )
        )

    result = await db.execute(stmt.limit(limit))
    rows = result.all()

    out = []
    for block, space in rows:
        out.append(
            SearchResult(
                type="block",
                id=str(block.id),
                display=str(block.network),
                name=block.name or None,
                status=None,
                description=block.description or None,
                hostname=None,
                mac_address=None,
                subnet_id=None,
                subnet_network=None,
                block_id=str(block.id),
                space_id=str(space.id),
                space_name=space.name,
            )
        )
    return out


async def _search_spaces(
    db: AsyncSession, q: str, limit: int
) -> list[SearchResult]:
    stmt = select(IPSpace).where(
        or_(
            IPSpace.name.ilike(f"%{q}%"),
            IPSpace.description.ilike(f"%{q}%"),
        )
    )
    result = await db.execute(stmt.limit(limit))
    spaces = result.scalars().all()

    out = []
    for space in spaces:
        out.append(
            SearchResult(
                type="space",
                id=str(space.id),
                display=space.name,
                name=space.name,
                status=None,
                description=space.description or None,
                hostname=None,
                mac_address=None,
                subnet_id=None,
                subnet_network=None,
                block_id=None,
                space_id=str(space.id),
                space_name=space.name,
            )
        )
    return out


async def _search_dns_groups(
    db: AsyncSession, q: str, limit: int
) -> list[SearchResult]:
    stmt = select(DNSServerGroup).where(
        or_(
            DNSServerGroup.name.ilike(f"%{q}%"),
            DNSServerGroup.description.ilike(f"%{q}%"),
        )
    )
    result = await db.execute(stmt.limit(limit))
    groups = result.scalars().all()

    return [
        SearchResult(
            type="dns_group",
            id=str(g.id),
            display=g.name,
            name=g.name,
            status=None,
            description=g.description or None,
            hostname=None,
            mac_address=None,
            subnet_id=None,
            subnet_network=None,
            block_id=None,
            space_id=None,
            space_name=None,
            dns_group_id=str(g.id),
            dns_group_name=g.name,
        )
        for g in groups
    ]


async def _search_dns_zones(
    db: AsyncSession, q: str, limit: int
) -> list[SearchResult]:
    stmt = (
        select(DNSZone, DNSServerGroup)
        .join(DNSServerGroup, DNSZone.group_id == DNSServerGroup.id)
        .where(DNSZone.name.ilike(f"%{q}%"))
    )
    result = await db.execute(stmt.limit(limit))
    rows = result.all()

    return [
        SearchResult(
            type="dns_zone",
            id=str(z.id),
            display=z.name,
            name=z.name,
            status=z.zone_type,
            description=None,
            hostname=None,
            mac_address=None,
            subnet_id=None,
            subnet_network=None,
            block_id=None,
            space_id=None,
            space_name=None,
            dns_group_id=str(g.id),
            dns_group_name=g.name,
            dns_zone_id=str(z.id),
            dns_zone_name=z.name,
        )
        for z, g in rows
    ]


async def _search_dns_records(
    db: AsyncSession, q: str, limit: int
) -> list[SearchResult]:
    stmt = (
        select(DNSRecord, DNSZone, DNSServerGroup)
        .join(DNSZone, DNSRecord.zone_id == DNSZone.id)
        .join(DNSServerGroup, DNSZone.group_id == DNSServerGroup.id)
        .where(
            or_(
                DNSRecord.fqdn.ilike(f"%{q}%"),
                DNSRecord.value.ilike(f"%{q}%"),
            )
        )
    )
    result = await db.execute(stmt.limit(limit))
    rows = result.all()

    return [
        SearchResult(
            type="dns_record",
            id=str(r.id),
            display=r.fqdn,
            name=r.fqdn,
            status=r.record_type,
            description=None,
            hostname=None,
            mac_address=None,
            subnet_id=None,
            subnet_network=None,
            block_id=None,
            space_id=None,
            space_name=None,
            dns_group_id=str(g.id),
            dns_group_name=g.name,
            dns_zone_id=str(z.id),
            dns_zone_name=z.name,
            dns_record_type=r.record_type,
            dns_record_value=r.value,
        )
        for r, z, g in rows
    ]


# ── Endpoint ───────────────────────────────────────────────────────────────────


@router.get("", response_model=SearchResponse)
async def global_search(
    current_user: CurrentUser,
    db: DB,
    q: str = Query(..., min_length=1, max_length=200, description="Search query"),
    types: str | None = Query(
        default=None,
        description="Comma-separated resource types: ip_address,subnet,block,space,dns_group,dns_zone,dns_record",
    ),
    limit: int = Query(default=25, ge=1, le=100),
) -> SearchResponse:
    """Search across IPAM and DNS resources.

    Query interpretation:
    - Valid IP (e.g. 10.0.0.1) → exact IP match + subnets/blocks containing it
    - CIDR (e.g. 10.0.0.0/24) → subnets/blocks matching the range
    - MAC address → IP addresses with that MAC
    - Text → hostname, name, FQDN, record value, description substring match
    """
    q = q.strip()
    requested = {t.strip() for t in types.split(",")} if types else None

    per_type_limit = max(limit, 10)
    results: list[SearchResult] = []

    if not requested or "ip_address" in requested:
        results.extend(await _search_addresses(db, q, per_type_limit))

    if not requested or "subnet" in requested:
        results.extend(await _search_subnets(db, q, per_type_limit))

    if not requested or "block" in requested:
        results.extend(await _search_blocks(db, q, per_type_limit))

    if not requested or "space" in requested:
        results.extend(await _search_spaces(db, q, per_type_limit))

    if not requested or "dns_group" in requested:
        results.extend(await _search_dns_groups(db, q, per_type_limit))

    if not requested or "dns_zone" in requested:
        results.extend(await _search_dns_zones(db, q, per_type_limit))

    if not requested or "dns_record" in requested:
        results.extend(await _search_dns_records(db, q, per_type_limit))

    # De-duplicate (same id can appear in multiple passes when q is an IP)
    seen: set[str] = set()
    deduped: list[SearchResult] = []
    for r in results:
        key = f"{r.type}:{r.id}"
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    logger.info(
        "search_executed",
        user=current_user.username,
        query=q,
        total=len(deduped),
    )

    return SearchResponse(query=q, total=len(deduped), results=deduped[:limit])
