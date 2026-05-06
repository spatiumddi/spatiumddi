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
