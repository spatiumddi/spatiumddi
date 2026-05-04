"""Read-only DNS tools for the Operator Copilot (issue #90 Wave 2)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.dns import DNSRecord, DNSServerGroup, DNSZone
from app.services.ai.tools.base import register_tool


class ListZonesArgs(BaseModel):
    group_id: str | None = Field(default=None, description="Filter by DNS server group UUID.")
    kind: str | None = Field(
        default=None,
        description="Filter by zone kind: 'forward' or 'reverse'.",
    )
    search: str | None = Field(
        default=None,
        description="Substring match on the zone name (FQDN).",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_dns_zones",
    description=(
        "List DNS zones (authoritative / secondary / stub / forward). "
        "Each summary includes name, type, kind (forward / reverse), "
        "TTL, server group, and view binding."
    ),
    args_model=ListZonesArgs,
    category="dns",
)
async def list_dns_zones(db: AsyncSession, user: User, args: ListZonesArgs) -> list[dict[str, Any]]:
    stmt = select(DNSZone).where(DNSZone.deleted_at.is_(None))
    if args.group_id:
        stmt = stmt.where(DNSZone.group_id == args.group_id)
    if args.kind:
        stmt = stmt.where(DNSZone.kind == args.kind)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(func.lower(DNSZone.name).like(like))
    stmt = stmt.order_by(DNSZone.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(z.id),
            "name": z.name,
            "zone_type": z.zone_type,
            "kind": z.kind,
            "group_id": str(z.group_id),
            "view_id": str(z.view_id) if z.view_id else None,
            "ttl": z.ttl,
        }
        for z in rows
    ]


class QueryRecordsArgs(BaseModel):
    zone_id: str | None = Field(default=None, description="Filter by zone UUID.")
    record_type: str | None = Field(
        default=None,
        description=(
            "Filter by record type — A, AAAA, CNAME, MX, TXT, NS, PTR, "
            "SRV, CAA, TLSA, SSHFP, NAPTR, LOC."
        ),
    )
    name: str | None = Field(
        default=None,
        description=(
            "Substring match on relative name OR full FQDN. Use this "
            "for questions like 'find all records for host1' or 'show "
            "me records under foo.example.com'."
        ),
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="query_dns_records",
    description=(
        "Search DNS records across zones. Filters: zone, record type, "
        "and name / FQDN substring. Returns each record's relative "
        "name, FQDN, type, value, TTL, and zone."
    ),
    args_model=QueryRecordsArgs,
    category="dns",
)
async def query_dns_records(
    db: AsyncSession, user: User, args: QueryRecordsArgs
) -> list[dict[str, Any]]:
    stmt = select(DNSRecord).where(DNSRecord.deleted_at.is_(None))
    if args.zone_id:
        stmt = stmt.where(DNSRecord.zone_id == args.zone_id)
    if args.record_type:
        stmt = stmt.where(DNSRecord.record_type == args.record_type.upper())
    if args.name:
        like = f"%{args.name.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(DNSRecord.name).like(like),
                func.lower(DNSRecord.fqdn).like(like),
            )
        )
    stmt = stmt.order_by(DNSRecord.fqdn.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "zone_id": str(r.zone_id),
            "name": r.name,
            "fqdn": r.fqdn,
            "record_type": r.record_type,
            "value": r.value,
            "ttl": r.ttl,
            "priority": r.priority,
            "weight": r.weight,
            "port": r.port,
        }
        for r in rows
    ]


class ListServerGroupsArgs(BaseModel):
    pass


@register_tool(
    name="list_dns_server_groups",
    description=(
        "List DNS server groups (logical groupings of authoritative "
        "DNS servers). Each summary includes name, group type, "
        "default view, and recursive flag."
    ),
    args_model=ListServerGroupsArgs,
    category="dns",
)
async def list_dns_server_groups(
    db: AsyncSession, user: User, args: ListServerGroupsArgs
) -> list[dict[str, Any]]:
    rows = (
        (await db.execute(select(DNSServerGroup).order_by(DNSServerGroup.name.asc())))
        .scalars()
        .all()
    )
    return [
        {
            "id": str(g.id),
            "name": g.name,
            "group_type": g.group_type,
            "default_view": g.default_view,
            "is_recursive": g.is_recursive,
        }
        for g in rows
    ]
