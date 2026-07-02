"""Read-only DNS tools for the Operator Copilot (issue #90 Wave 2)."""

from __future__ import annotations

import asyncio
import ipaddress
import uuid
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
    DNSServerOptions,
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
        return {
            "name": target,
            "rdtype": args.rdtype,
            "rcode": "NXDOMAIN",
            "answers": [],
        }
    except dns.resolver.NoAnswer:
        return {
            "name": target,
            "rdtype": args.rdtype,
            "rcode": "NOERROR",
            "answers": [],
        }
    except dns.resolver.NoNameservers as exc:
        return {
            "name": target,
            "rdtype": args.rdtype,
            "error": f"no nameservers: {exc}",
        }
    except dns.exception.Timeout:
        return {"name": target, "rdtype": args.rdtype, "error": "resolver timeout"}
    except dns.exception.DNSException as exc:
        return {
            "name": target,
            "rdtype": args.rdtype,
            "error": f"{type(exc).__name__}: {exc}",
        }
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
        return {
            "address": args.address,
            "arpa": arpa,
            "rcode": "NXDOMAIN",
            "answers": [],
        }
    except dns.resolver.NoAnswer:
        return {
            "address": args.address,
            "arpa": arpa,
            "rcode": "NOERROR",
            "answers": [],
        }
    except dns.exception.Timeout:
        return {"address": args.address, "arpa": arpa, "error": "resolver timeout"}
    except dns.exception.DNSException as exc:
        return {
            "address": args.address,
            "arpa": arpa,
            "error": f"{type(exc).__name__}: {exc}",
        }
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
            "last_synced_at": (r.last_synced_at.isoformat() if r.last_synced_at else None),
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
            "last_checked_at": (p.last_checked_at.isoformat() if p.last_checked_at else None),
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


class FindZoneDNSSECInfoArgs(BaseModel):
    zone_id: uuid.UUID = Field(
        description="UUID of the dns_zone row to inspect.",
    )


@register_tool(
    name="find_zone_dnssec_info",
    description=(
        "Return the DNSSEC posture of one DNS zone: ``dnssec_enabled`` "
        "flag, the list of DS records (key tag, algorithm, digest "
        "type, digest — formatted for parent-registrar paste), and "
        "the ``dnssec_synced_at`` timestamp the agent last reported. "
        "When enabled but ``dnssec_synced_at`` is null the zone is "
        "mid-signing or the agent hasn't reported back yet. Use this "
        "to answer 'is example.com signed?' or 'give me the DS "
        "records to paste into the registrar'. Read-only — the "
        "matching ``propose_sign_zone_dnssec`` write is deferred."
    ),
    args_model=FindZoneDNSSECInfoArgs,
    category="dns",
)
async def find_zone_dnssec_info(
    db: AsyncSession, user: User, args: FindZoneDNSSECInfoArgs
) -> dict[str, Any]:
    from app.models.dns import DNSKey  # noqa: PLC0415

    zone = await db.get(DNSZone, args.zone_id)
    if zone is None:
        return {"error": "DNS zone not found", "zone_id": str(args.zone_id)}
    keys = (await db.execute(select(DNSKey).where(DNSKey.zone_id == zone.id))).scalars().all()
    return {
        "zone_id": str(zone.id),
        "name": zone.name,
        "dnssec_enabled": zone.dnssec_enabled,
        "dnssec_policy_id": (str(zone.dnssec_policy_id) if zone.dnssec_policy_id else None),
        "dnssec_ds_records": zone.dnssec_ds_records,
        "dnssec_synced_at": (zone.dnssec_synced_at.isoformat() if zone.dnssec_synced_at else None),
        "last_serial": zone.last_serial,
        "keys": [
            {
                "key_tag": k.key_tag,
                "key_type": k.key_type,
                "algorithm": k.algorithm,
                "state": k.state,
                "ds_records": k.ds_records or [],
            }
            for k in keys
        ],
    }


class FindDNSRateLimitSettingsArgs(BaseModel):
    group_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of a dns_server_group to inspect. Omit for all groups.",
    )


@register_tool(
    name="find_dns_rate_limit_settings",
    description=(
        "Return the BIND9 Response Rate Limiting (RRL) + amplification "
        "defense posture for one DNS server group (or all groups when "
        "group_id is omitted): whether RRL is enabled, responses-per-second "
        "/ window / slip / qps-scale, the exempt-clients list, log-only "
        "(dry-run) mode, and the amplification knobs (minimal-responses, "
        "tcp-clients, clients-per-query, max-clients-per-query). Use this to "
        "answer 'is rate limiting on for the prod DNS group?' or 'what's the "
        "RRL responses-per-second?'. Read-only."
    ),
    args_model=FindDNSRateLimitSettingsArgs,
    category="dns",
)
async def find_dns_rate_limit_settings(
    db: AsyncSession, user: User, args: FindDNSRateLimitSettingsArgs
) -> dict[str, Any]:
    # LEFT JOIN from the group: a DNSServerOptions row is created lazily (on
    # first GET/PUT of options), so an inner join would silently omit any
    # group that hasn't materialised one yet. Those groups report the model
    # defaults (RRL off) — which is their effective posture.
    stmt = select(DNSServerGroup, DNSServerOptions).outerjoin(
        DNSServerOptions, DNSServerOptions.group_id == DNSServerGroup.id
    )
    if args.group_id is not None:
        stmt = stmt.where(DNSServerGroup.id == args.group_id)
    rows = (await db.execute(stmt)).all()

    def _defaulted(g: DNSServerGroup, o: DNSServerOptions | None) -> dict[str, Any]:
        if o is None:
            return {
                "group_id": str(g.id),
                "group_name": g.name,
                "options_row_exists": False,
                "rrl_enabled": False,
                "rrl_responses_per_second": 15,
                "rrl_window": 15,
                "rrl_slip": 2,
                "rrl_qps_scale": None,
                "rrl_exempt_clients": [],
                "rrl_log_only": False,
                "minimal_responses": False,
                "tcp_clients": None,
                "clients_per_query": None,
                "max_clients_per_query": None,
                "dnsdist_enabled": False,
                "dnsdist_max_qps_per_client": None,
                "dnsdist_action": "truncate",
                "dnsdist_dynblock_qps": None,
                "dnsdist_dynblock_seconds": 60,
            }
        return {
            "group_id": str(g.id),
            "group_name": g.name,
            "options_row_exists": True,
            "rrl_enabled": o.rrl_enabled,
            "rrl_responses_per_second": o.rrl_responses_per_second,
            "rrl_window": o.rrl_window,
            "rrl_slip": o.rrl_slip,
            "rrl_qps_scale": o.rrl_qps_scale,
            "rrl_exempt_clients": o.rrl_exempt_clients or [],
            "rrl_log_only": o.rrl_log_only,
            "minimal_responses": o.minimal_responses,
            "tcp_clients": o.tcp_clients,
            "clients_per_query": o.clients_per_query,
            "max_clients_per_query": o.max_clients_per_query,
            "dnsdist_enabled": o.dnsdist_enabled,
            "dnsdist_max_qps_per_client": o.dnsdist_max_qps_per_client,
            "dnsdist_action": o.dnsdist_action,
            "dnsdist_dynblock_qps": o.dnsdist_dynblock_qps,
            "dnsdist_dynblock_seconds": o.dnsdist_dynblock_seconds,
        }

    groups = [_defaulted(g, o) for g, o in rows]
    return {"count": len(groups), "groups": groups}


class FindZoneDriftArgs(BaseModel):
    zone_id: uuid.UUID = Field(
        description="UUID of the dns_zone to check for per-server config drift.",
    )


@register_tool(
    name="find_dns_zone_drift",
    description=(
        "Per-server config-drift report for one DNS zone (#61): AXFRs / "
        "pulls the live zone from every server in the zone's group and "
        "diffs it against the SpatiumDDI DB source of truth. Returns, per "
        "server, how many records are 'extra on the server' (a manual "
        "change made directly on the host), 'missing on the server' (DB "
        "rows the server isn't serving), and in-sync — plus a sample of the "
        "drifting records. A value change shows as a missing+extra pair. "
        "Use to answer 'is example.com drifting?' or 'did someone edit "
        "records directly on the BIND9 host?'. Read-only."
    ),
    args_model=FindZoneDriftArgs,
    category="dns",
)
async def find_dns_zone_drift(
    db: AsyncSession, user: User, args: FindZoneDriftArgs
) -> dict[str, Any]:
    from app.services.dns.drift import compute_zone_drift  # noqa: PLC0415

    zone = await db.get(DNSZone, args.zone_id)
    if zone is None:
        return {"error": "DNS zone not found", "zone_id": str(args.zone_id)}
    report = await compute_zone_drift(db, group_id=zone.group_id, zone=zone)
    return {
        "zone_id": report.zone_id,
        "name": report.zone_name,
        "db_record_count": report.db_record_count,
        "servers": [
            {
                "server_name": s.server_name,
                "driver": s.driver,
                "status": s.status,
                "error": s.error,
                "in_sync": s.in_sync,
                "drift_count": s.drift_count,
                "extra_on_server": [
                    f"{r.name} {r.record_type} {r.value}" for r in s.extra_on_server[:20]
                ],
                "missing_on_server": [
                    f"{r.name} {r.record_type} {r.value}" for r in s.missing_on_server[:20]
                ],
            }
            for s in report.servers
        ],
    }


class ListDNSSECPoliciesArgs(BaseModel):
    pass


@register_tool(
    name="list_dnssec_policies",
    description=(
        "List the DNSSEC signing policies operators can attach to BIND9 "
        "zones (issue #49): name, algorithm, NSEC3 settings, and KSK/ZSK "
        "lifetimes. The built-in 'default' policy always exists. Use this "
        "to answer 'what DNSSEC policies are available?' or 'what algorithm "
        "does policy X use?'. Read-only."
    ),
    args_model=ListDNSSECPoliciesArgs,
    category="dns",
)
async def list_dnssec_policies(
    db: AsyncSession, user: User, args: ListDNSSECPoliciesArgs
) -> dict[str, Any]:
    from app.models.dns import DNSSECPolicy  # noqa: PLC0415

    rows = (await db.execute(select(DNSSECPolicy).order_by(DNSSECPolicy.name))).scalars().all()
    return {
        "policies": [
            {
                "id": str(p.id),
                "name": p.name,
                "is_builtin": p.is_builtin,
                "algorithm": p.algorithm,
                "ksk_lifetime_days": p.ksk_lifetime_days,
                "zsk_lifetime_days": p.zsk_lifetime_days,
                "nsec3": p.nsec3,
                "nsec3_iterations": p.nsec3_iterations,
                "nsec3_salt_length": p.nsec3_salt_length,
                "nsec3_optout": p.nsec3_optout,
            }
            for p in rows
        ]
    }


class FindDNSQueryStatsArgs(BaseModel):
    server_id: str | None = Field(
        default=None,
        description="Filter to one DNS server UUID. Omit for every server.",
    )
    window_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description="Trailing window to summarise (default 15 min, max 24 h).",
    )


@register_tool(
    name="find_dns_query_stats",
    description=(
        "Per-server DNS query stats over a trailing window from "
        "dns_metric_sample (the same rcode counters the NXDOMAIN-spike / "
        "query-rate-spike alerts use): total queries, NOERROR / NXDOMAIN / "
        "SERVFAIL counts, and the NXDOMAIN ratio %. Use to answer 'is any "
        "DNS server spiking?', 'what's the NXDOMAIN rate right now?', or to "
        "triage a query-anomaly alert. Read-only; empty for servers with no "
        "metric samples (non-agent / no traffic)."
    ),
    args_model=FindDNSQueryStatsArgs,
    category="dns",
)
async def find_dns_query_stats(
    db: AsyncSession, user: User, args: FindDNSQueryStatsArgs
) -> list[dict[str, Any]]:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from app.models.dns import DNSServer  # noqa: PLC0415
    from app.models.metrics import DNSMetricSample  # noqa: PLC0415

    since = datetime.now(UTC) - timedelta(minutes=args.window_minutes)
    stmt = (
        select(
            DNSMetricSample.server_id,
            func.sum(DNSMetricSample.queries_total).label("q"),
            func.sum(DNSMetricSample.noerror).label("ne"),
            func.sum(DNSMetricSample.nxdomain).label("nx"),
            func.sum(DNSMetricSample.servfail).label("sf"),
        )
        .where(DNSMetricSample.bucket_at >= since)
        .group_by(DNSMetricSample.server_id)
    )
    if args.server_id:
        stmt = stmt.where(DNSMetricSample.server_id == args.server_id)
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []
    names = {
        sid: name
        for sid, name in (
            await db.execute(
                select(DNSServer.id, DNSServer.name).where(
                    DNSServer.id.in_([r.server_id for r in rows])
                )
            )
        ).all()
    }
    out: list[dict[str, Any]] = []
    for r in rows:
        q = int(r.q or 0)
        nx = int(r.nx or 0)
        out.append(
            {
                "server_id": str(r.server_id),
                "server_name": names.get(r.server_id),
                "window_minutes": args.window_minutes,
                "queries_total": q,
                "noerror": int(r.ne or 0),
                "nxdomain": nx,
                "servfail": int(r.sf or 0),
                "nxdomain_ratio_pct": round(nx / q * 100, 1) if q > 0 else 0.0,
            }
        )
    out.sort(key=lambda d: d["nxdomain_ratio_pct"], reverse=True)
    return out


# Silence false-positive on lifted imports — Python pulls them in at
# module load, but the linters want at-least-one referent in module
# scope.
_ = (DNSPoolMember, DNSServerGroup)
