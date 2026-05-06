"""Read-only DNS tools for the Operator Copilot (issue #90 Wave 2)."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any

import dns.exception
import dns.resolver
import dns.reversename
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.dns import (
    DNSBlockList,
    DNSPool,
    DNSPoolMember,
    DNSRecord,
    DNSServerGroup,
    DNSView,
    DNSZone,
)
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


# ── Live DNS lookup tools ───────────────────────────────────────────
#
# ``forward_dns`` and ``reverse_dns`` wrap dnspython so the operator
# can ask "what does the resolver actually return for hostname X?"
# without leaving the chat. Configurable resolver lets operators
# point at a SpatiumDDI-managed BIND9 view they can't easily query
# from their workstation.


_DEFAULT_RESOLVE_TIMEOUT = 5.0


def _build_resolver(servers: list[str] | None) -> dns.resolver.Resolver:
    resolver = dns.resolver.Resolver(configure=not servers)
    if servers:
        resolver.nameservers = servers
    resolver.lifetime = _DEFAULT_RESOLVE_TIMEOUT
    return resolver


class ForwardDnsArgs(BaseModel):
    name: str = Field(
        ...,
        description="Hostname / FQDN to resolve. Trailing dots are tolerated.",
    )
    rdtype: str = Field(
        default="A",
        description=(
            "Record type — A, AAAA, CNAME, MX, NS, TXT, SOA, SRV, CAA. "
            "Pick A for v4 forward, AAAA for v6, ANY only when the "
            "operator explicitly wants every record at the name."
        ),
    )
    servers: list[str] | None = Field(
        default=None,
        description=(
            "Optional resolver IPs (e.g. ['10.0.0.53']). Defaults to "
            "the host's /etc/resolv.conf — useful when querying the "
            "platform's own BIND9 view from outside it."
        ),
    )

    @field_validator("rdtype")
    @classmethod
    def upper_rdtype(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("rdtype is required")
        return v


@register_tool(
    name="forward_dns",
    description=(
        "Live forward DNS lookup ('dig <name> <rdtype>'). Resolves "
        "against the host's resolver by default, or against operator-"
        "supplied nameserver IPs. Returns every answer record verbatim. "
        "Use this when the operator wants ground truth from the "
        "resolver — DB lookups via list_dns_records show the configured "
        "intent; this shows what the world actually sees."
    ),
    args_model=ForwardDnsArgs,
    category="dns",
    default_enabled=False,
)
async def forward_dns(
    db: AsyncSession,  # noqa: ARG001
    user: User,  # noqa: ARG001
    args: ForwardDnsArgs,
) -> dict[str, Any]:
    target = args.name.strip().rstrip(".")
    resolver = _build_resolver(args.servers)
    try:
        answers = await asyncio.to_thread(resolver.resolve, target, args.rdtype)
    except dns.resolver.NXDOMAIN:
        return {"name": target, "rdtype": args.rdtype, "rcode": "NXDOMAIN", "answers": []}
    except dns.resolver.NoAnswer:
        return {"name": target, "rdtype": args.rdtype, "rcode": "NOERROR", "answers": []}
    except dns.resolver.NoNameservers as exc:
        return {"name": target, "rdtype": args.rdtype, "error": f"no nameservers: {exc}"}
    except dns.exception.Timeout:
        return {"name": target, "rdtype": args.rdtype, "error": "resolver timeout"}
    except dns.exception.DNSException as exc:
        return {"name": target, "rdtype": args.rdtype, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "name": target,
        "rdtype": args.rdtype,
        "rcode": "NOERROR",
        "ttl": int(answers.rrset.ttl) if answers.rrset is not None else None,
        "answers": [str(a) for a in answers],
    }


class ReverseDnsArgs(BaseModel):
    address: str = Field(
        ...,
        description="IPv4 or IPv6 address. Built into the appropriate ``in-addr.arpa`` / ``ip6.arpa`` query.",
    )
    servers: list[str] | None = Field(
        default=None,
        description="Optional resolver IPs (see ``forward_dns``).",
    )

    @field_validator("address")
    @classmethod
    def valid_addr(cls, v: str) -> str:
        try:
            ipaddress.ip_address(v.strip())
        except ValueError as exc:
            raise ValueError("Invalid IP address") from exc
        return v.strip()


@register_tool(
    name="reverse_dns",
    description=(
        "Live reverse-DNS lookup. Resolves the appropriate "
        "``<addr>.in-addr.arpa`` / ``<addr>.ip6.arpa`` PTR record. "
        "Returns every PTR answer; an empty list means no PTR exists. "
        "Useful when the operator wants to confirm reverse delegation "
        "is wired up correctly."
    ),
    args_model=ReverseDnsArgs,
    category="dns",
    default_enabled=False,
)
async def reverse_dns(
    db: AsyncSession,  # noqa: ARG001
    user: User,  # noqa: ARG001
    args: ReverseDnsArgs,
) -> dict[str, Any]:
    arpa = dns.reversename.from_address(args.address).to_text(omit_final_dot=True)
    resolver = _build_resolver(args.servers)
    try:
        answers = await asyncio.to_thread(resolver.resolve, arpa, "PTR")
    except dns.resolver.NXDOMAIN:
        return {"address": args.address, "arpa": arpa, "rcode": "NXDOMAIN", "answers": []}
    except dns.resolver.NoAnswer:
        return {"address": args.address, "arpa": arpa, "rcode": "NOERROR", "answers": []}
    except dns.exception.Timeout:
        return {"address": args.address, "arpa": arpa, "error": "resolver timeout"}
    except dns.exception.DNSException as exc:
        return {"address": args.address, "arpa": arpa, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "address": args.address,
        "arpa": arpa,
        "rcode": "NOERROR",
        "answers": [str(a).rstrip(".") for a in answers],
    }


# ── Tier 3 DNS sub-resource depth (issue #101) ────────────────────────


# ── list_dns_records (cross-zone) ─────────────────────────────────────


class ListDNSRecordsArgs(BaseModel):
    name_contains: str | None = Field(
        default=None,
        description="Substring match on the relative record name (e.g. 'api' to find 'api.*').",
    )
    fqdn_contains: str | None = Field(
        default=None,
        description="Substring match on the full FQDN (e.g. 'foo.example.com').",
    )
    record_type: str | None = Field(
        default=None,
        description="Filter by type (A / AAAA / CNAME / MX / TXT / …). Case-insensitive.",
    )
    value_contains: str | None = Field(
        default=None,
        description="Substring match on the right-hand-side value (target IP / FQDN / TXT).",
    )
    zone_id: str | None = Field(default=None, description="Restrict to one zone by UUID.")
    group_id: str | None = Field(
        default=None,
        description="Restrict to zones under one DNS server group by UUID.",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_dns_records",
    description=(
        "Cross-zone DNS record search. Filterable by relative name "
        "substring, FQDN substring, type, value substring, zone, or "
        "server group. Each row carries id, zone_id + zone name, "
        "name (relative), fqdn, record_type, value, ttl, priority, "
        "and the auto_generated flag (rows mirrored from IPAM / "
        "Kubernetes / Tailscale carry True). Use for 'where does "
        "*.api point?', 'find every CNAME pointing at "
        "old-host.example.com', or 'show me TXT records mentioning "
        "verification'. Distinct from query_dns_records, which is "
        "single-zone."
    ),
    args_model=ListDNSRecordsArgs,
    category="dns",
)
async def list_dns_records(
    db: AsyncSession, user: User, args: ListDNSRecordsArgs
) -> list[dict[str, Any]]:
    stmt = (
        select(DNSRecord, DNSZone.name.label("zone_name"))
        .join(DNSZone, DNSZone.id == DNSRecord.zone_id)
        .where(DNSRecord.deleted_at.is_(None))
        .where(DNSZone.deleted_at.is_(None))
    )
    if args.name_contains:
        stmt = stmt.where(func.lower(DNSRecord.name).like(f"%{args.name_contains.lower()}%"))
    if args.fqdn_contains:
        stmt = stmt.where(func.lower(DNSRecord.fqdn).like(f"%{args.fqdn_contains.lower()}%"))
    if args.record_type:
        stmt = stmt.where(DNSRecord.record_type == args.record_type.upper())
    if args.value_contains:
        stmt = stmt.where(func.lower(DNSRecord.value).like(f"%{args.value_contains.lower()}%"))
    if args.zone_id:
        stmt = stmt.where(DNSRecord.zone_id == args.zone_id)
    if args.group_id:
        stmt = stmt.where(DNSZone.group_id == args.group_id)
    stmt = stmt.order_by(DNSRecord.fqdn.asc(), DNSRecord.record_type.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": str(r.DNSRecord.id),
            "zone_id": str(r.DNSRecord.zone_id),
            "zone_name": r.zone_name,
            "name": r.DNSRecord.name,
            "fqdn": r.DNSRecord.fqdn,
            "record_type": r.DNSRecord.record_type,
            "value": r.DNSRecord.value,
            "ttl": r.DNSRecord.ttl,
            "priority": r.DNSRecord.priority,
            "auto_generated": r.DNSRecord.auto_generated,
        }
        for r in rows
    ]


# ── list_dns_blocklists ───────────────────────────────────────────────


class ListDNSBlockListsArgs(BaseModel):
    search: str | None = Field(
        default=None,
        description="Substring match on blocklist name or description.",
    )
    category: str | None = Field(
        default=None,
        description="Filter by category: ads / malware / tracking / adult / custom / …",
    )
    enabled: bool | None = Field(
        default=None, description="Filter by ``enabled`` flag. None = both."
    )
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="list_dns_blocklists",
    description=(
        "List DNS blocklists (RPZ rows). Each carries id, name, "
        "description, category, source_type (manual / url / "
        "file_upload), feed_url + feed_format when remote, "
        "block_mode (nxdomain / sinkhole / refused), enabled, "
        "entry_count, last_synced_at + last_sync_status / error. "
        "Use for 'which blocklists are active?', 'is the malware "
        "feed up to date?', or 'when did the ads blocklist last "
        "sync?'."
    ),
    args_model=ListDNSBlockListsArgs,
    category="dns",
)
async def list_dns_blocklists(
    db: AsyncSession, user: User, args: ListDNSBlockListsArgs
) -> list[dict[str, Any]]:
    stmt = select(DNSBlockList)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(DNSBlockList.name).like(like),
                func.lower(DNSBlockList.description).like(like),
            )
        )
    if args.category:
        stmt = stmt.where(DNSBlockList.category == args.category.lower())
    if args.enabled is not None:
        stmt = stmt.where(DNSBlockList.enabled.is_(args.enabled))
    stmt = stmt.order_by(DNSBlockList.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "description": r.description,
            "category": r.category,
            "source_type": r.source_type,
            "feed_url": r.feed_url,
            "feed_format": r.feed_format,
            "update_interval_hours": r.update_interval_hours,
            "block_mode": r.block_mode,
            "sinkhole_ip": r.sinkhole_ip,
            "enabled": r.enabled,
            "entry_count": r.entry_count,
            "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
            "last_sync_status": r.last_sync_status,
            "last_sync_error": r.last_sync_error,
        }
        for r in rows
    ]


# ── list_dns_pools ────────────────────────────────────────────────────


class ListDNSPoolsArgs(BaseModel):
    search: str | None = Field(
        default=None,
        description="Substring match on pool name or record_name.",
    )
    zone_id: str | None = Field(default=None, description="Restrict to one zone by UUID.")
    group_id: str | None = Field(
        default=None, description="Restrict to one DNS server group by UUID."
    )
    enabled: bool | None = Field(default=None, description="Filter by ``enabled`` flag.")
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="list_dns_pools",
    description=(
        "List GSLB pools (health-checked A/AAAA target sets sharing "
        "one DNS name). Each row carries id, name, description, "
        "zone_id, record_name + record_type, ttl, enabled, "
        "hc_type / interval / threshold settings, last_checked_at, "
        "and the per-member breakdown (address / weight / enabled / "
        "last_check_state / last_check_error). Use for 'is the "
        "www pool healthy?', 'which member of the api pool is "
        "down?', or 'what's the TTL on the gslb pool?'."
    ),
    args_model=ListDNSPoolsArgs,
    category="dns",
)
async def list_dns_pools(
    db: AsyncSession, user: User, args: ListDNSPoolsArgs
) -> list[dict[str, Any]]:
    # Eager-load members so each pool returns its full breakdown in
    # one trip — pools rarely exceed a handful of members.
    from sqlalchemy.orm import selectinload  # local import keeps the top imports lean

    stmt = select(DNSPool).options(selectinload(DNSPool.members))
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(DNSPool.name).like(like),
                func.lower(DNSPool.record_name).like(like),
            )
        )
    if args.zone_id:
        stmt = stmt.where(DNSPool.zone_id == args.zone_id)
    if args.group_id:
        stmt = stmt.where(DNSPool.group_id == args.group_id)
    if args.enabled is not None:
        stmt = stmt.where(DNSPool.enabled.is_(args.enabled))
    stmt = stmt.order_by(DNSPool.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().unique().all()
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
            "zone_id": str(p.zone_id),
            "group_id": str(p.group_id),
            "record_name": p.record_name,
            "record_type": p.record_type,
            "ttl": p.ttl,
            "enabled": p.enabled,
            "hc_type": p.hc_type,
            "hc_target_port": p.hc_target_port,
            "hc_interval_seconds": p.hc_interval_seconds,
            "hc_unhealthy_threshold": p.hc_unhealthy_threshold,
            "hc_healthy_threshold": p.hc_healthy_threshold,
            "last_checked_at": p.last_checked_at.isoformat() if p.last_checked_at else None,
            "members": [
                {
                    "address": m.address,
                    "weight": m.weight,
                    "enabled": m.enabled,
                    "last_check_state": m.last_check_state,
                    "last_check_at": (m.last_check_at.isoformat() if m.last_check_at else None),
                    "last_check_error": m.last_check_error,
                }
                for m in p.members
            ],
        }
        for p in rows
    ]


# ── list_dns_views ────────────────────────────────────────────────────


class ListDNSViewsArgs(BaseModel):
    search: str | None = Field(default=None, description="Substring match on view name.")
    group_id: str | None = Field(
        default=None, description="Restrict to one DNS server group by UUID."
    )
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="list_dns_views",
    description=(
        "List split-horizon DNS views — different clients see "
        "different zone data. Each row carries id, name, "
        "description, group_id, match_clients (CIDR/ACL list), "
        "match_destinations, recursion flag, evaluation order, and "
        "any per-view allow_query / allow_query_cache overrides. "
        "Use for 'which views does the corp group have?' or 'what "
        "clients does the internal view match?'."
    ),
    args_model=ListDNSViewsArgs,
    category="dns",
)
async def list_dns_views(
    db: AsyncSession, user: User, args: ListDNSViewsArgs
) -> list[dict[str, Any]]:
    stmt = select(DNSView)
    if args.search:
        stmt = stmt.where(func.lower(DNSView.name).like(f"%{args.search.lower()}%"))
    if args.group_id:
        stmt = stmt.where(DNSView.group_id == args.group_id)
    stmt = stmt.order_by(DNSView.order.asc(), DNSView.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(v.id),
            "name": v.name,
            "description": v.description,
            "group_id": str(v.group_id),
            "match_clients": v.match_clients,
            "match_destinations": v.match_destinations,
            "recursion": v.recursion,
            "order": v.order,
            "allow_query": v.allow_query,
            "allow_query_cache": v.allow_query_cache,
        }
        for v in rows
    ]


# Silence false-positive on lifted imports — Python pulls them in at
# module load, but the linters want at-least-one referent in module
# scope.
_ = (DNSPoolMember, DNSServerGroup)
