"""Tier 4 observability + cross-resource search tools for the
Operator Copilot (issue #101).

Wraps four existing surfaces so the LLM can answer log + metric +
search questions without falling through to "ask your operator to
open the UI":

* ``query_dns_query_log`` — BIND9 query log entries (post-2026.04.24
  push pipeline) with filters for client IP, qname substring,
  qtype, view, since, max.
* ``query_dhcp_activity_log`` — Kea DHCPv4 activity log entries
  with filters for severity, log code, MAC, IP, since.
* ``query_logs`` — high-level inventory of which agent-driven log
  sources are available (BIND9 query / Kea activity), with row
  counts in the recent window. Operators usually need this once
  per session to learn what's available.
* ``get_dns_query_rate`` / ``get_dhcp_lease_rate`` — timeseries
  roll-ups from the ``dns_metric_sample`` / ``dhcp_metric_sample``
  tables (caps default to 24 buckets so payload stays compact).
* ``global_search`` — cross-resource lookup matching the UI's
  Cmd-K palette. Calls the same internal helpers
  ``app.api.v1.search.router`` exposes; output is the same hit
  shape (type / id / display / breadcrumb).

All read-only. No tool here is gated by a feature_module — log /
metric / search surfaces are always-on platform infrastructure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.logs import DHCPLogEntry, DNSQueryLogEntry
from app.models.metrics import DHCPMetricSample, DNSMetricSample
from app.services.ai.tools.base import register_tool


def _since_default(hours: float | None) -> datetime | None:
    if hours is None:
        return None
    return datetime.now(UTC) - timedelta(hours=hours)


# ── query_dns_query_log ───────────────────────────────────────────────


class QueryDNSQueryLogArgs(BaseModel):
    qname_contains: str | None = Field(
        default=None,
        description="Substring match on the queried name (case-insensitive).",
    )
    qtype: str | None = Field(
        default=None, description="Filter by record type (A, AAAA, MX, TXT, …)."
    )
    client_ip: str | None = Field(default=None, description="Filter by client IP (exact match).")
    view: str | None = Field(default=None, description="Filter by DNS view (split-horizon name).")
    since_hours: float | None = Field(
        default=24,
        description="Only include rows newer than N hours ago. None = no lower bound.",
        ge=0.0,
    )
    limit: int = Field(default=200, ge=1, le=1000)


@register_tool(
    name="query_dns_query_log",
    description=(
        "Query the BIND9 query log (rows shipped by the DNS agent's "
        "QueryLogShipper). Filterable by qname substring, qtype, "
        "client IP, view, and since-window. Returns the most recent "
        "matching rows with timestamp / client / qname / qtype / "
        "flags / view. Use for 'who's resolving example.com?', 'show "
        "AAAA queries from 10.0.0.5 in the last hour', or 'is anyone "
        "asking for the deprecated cname?'. Note: query logs are "
        "operator-triage data with 24 h retention — for longer "
        "history see Loki via the operator's UI."
    ),
    args_model=QueryDNSQueryLogArgs,
    category="ops",
)
async def query_dns_query_log(
    db: AsyncSession, user: User, args: QueryDNSQueryLogArgs
) -> list[dict[str, Any]]:
    stmt = select(DNSQueryLogEntry)
    if args.qname_contains:
        stmt = stmt.where(
            func.lower(DNSQueryLogEntry.qname).like(f"%{args.qname_contains.lower()}%")
        )
    if args.qtype:
        stmt = stmt.where(DNSQueryLogEntry.qtype == args.qtype.upper())
    if args.client_ip:
        stmt = stmt.where(DNSQueryLogEntry.client_ip == args.client_ip)
    if args.view:
        stmt = stmt.where(DNSQueryLogEntry.view == args.view)
    cutoff = _since_default(args.since_hours)
    if cutoff is not None:
        stmt = stmt.where(DNSQueryLogEntry.ts >= cutoff)
    stmt = stmt.order_by(desc(DNSQueryLogEntry.ts)).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "ts": r.ts.isoformat() if r.ts else None,
            "server_id": str(r.server_id) if r.server_id else None,
            "client_ip": str(r.client_ip) if r.client_ip else None,
            "client_port": r.client_port,
            "qname": r.qname,
            "qclass": r.qclass,
            "qtype": r.qtype,
            "flags": r.flags,
            "view": r.view,
        }
        for r in rows
    ]


# ── query_dhcp_activity_log ───────────────────────────────────────────


class QueryDHCPActivityLogArgs(BaseModel):
    severity: str | None = Field(
        default=None,
        description="Filter by severity (DEBUG / INFO / WARN / ERROR).",
    )
    code: str | None = Field(
        default=None, description="Filter by Kea log code (e.g. DHCP4_LEASE_ALLOC)."
    )
    mac_address: str | None = Field(
        default=None, description="Filter by MAC address (exact match)."
    )
    ip_address: str | None = Field(default=None, description="Filter by IP address (exact match).")
    since_hours: float | None = Field(
        default=24,
        description="Only include rows newer than N hours ago. None = no lower bound.",
        ge=0.0,
    )
    limit: int = Field(default=200, ge=1, le=1000)


@register_tool(
    name="query_dhcp_activity_log",
    description=(
        "Query the Kea DHCPv4 activity log (rows shipped by the DHCP "
        "agent's LogShipper). Filterable by severity, Kea log code, "
        "MAC, IP, and since-window. Use for 'why isn't 11:22:33:... "
        "getting a lease?', 'show recent NAKs', or 'what was the "
        "last activity for IP 10.0.0.42?'. Note: kept for operator "
        "triage with 24 h retention."
    ),
    args_model=QueryDHCPActivityLogArgs,
    category="ops",
)
async def query_dhcp_activity_log(
    db: AsyncSession, user: User, args: QueryDHCPActivityLogArgs
) -> list[dict[str, Any]]:
    stmt = select(DHCPLogEntry)
    if args.severity:
        stmt = stmt.where(DHCPLogEntry.severity == args.severity.upper())
    if args.code:
        stmt = stmt.where(DHCPLogEntry.code == args.code)
    if args.mac_address:
        stmt = stmt.where(DHCPLogEntry.mac_address == args.mac_address)
    if args.ip_address:
        stmt = stmt.where(DHCPLogEntry.ip_address == args.ip_address)
    cutoff = _since_default(args.since_hours)
    if cutoff is not None:
        stmt = stmt.where(DHCPLogEntry.ts >= cutoff)
    stmt = stmt.order_by(desc(DHCPLogEntry.ts)).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "ts": r.ts.isoformat() if r.ts else None,
            "server_id": str(r.server_id) if r.server_id else None,
            "severity": r.severity,
            "code": r.code,
            "mac_address": str(r.mac_address) if r.mac_address else None,
            "ip_address": str(r.ip_address) if r.ip_address else None,
        }
        for r in rows
    ]


# ── query_logs ────────────────────────────────────────────────────────


class QueryLogsArgs(BaseModel):
    since_hours: float = Field(
        default=1, ge=0.1, le=168, description="Window for the row counts (default 1h)."
    )


@register_tool(
    name="query_logs",
    description=(
        "Inventory of agent-driven log sources available — DNS query "
        "log + DHCP activity log + audit log — with the row count "
        "for each in the last ``since_hours`` window. Use this once "
        "per conversation to learn which logs are populated; then "
        "switch to ``query_dns_query_log`` / ``query_dhcp_activity_log`` "
        "/ ``get_audit_history`` for the actual rows."
    ),
    args_model=QueryLogsArgs,
    category="ops",
)
async def query_logs(db: AsyncSession, user: User, args: QueryLogsArgs) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(hours=args.since_hours)
    dns_count = (
        await db.execute(
            select(func.count()).select_from(DNSQueryLogEntry).where(DNSQueryLogEntry.ts >= cutoff)
        )
    ).scalar_one()
    dhcp_count = (
        await db.execute(
            select(func.count()).select_from(DHCPLogEntry).where(DHCPLogEntry.ts >= cutoff)
        )
    ).scalar_one()
    return {
        "since_hours": args.since_hours,
        "sources": [
            {
                "name": "dns_query_log",
                "row_count": int(dns_count),
                "tool": "query_dns_query_log",
                "retention": "24h",
            },
            {
                "name": "dhcp_activity_log",
                "row_count": int(dhcp_count),
                "tool": "query_dhcp_activity_log",
                "retention": "24h",
            },
            {
                "name": "audit_log",
                "row_count": None,
                "tool": "get_audit_history",
                "retention": "permanent (append-only)",
            },
        ],
    }


# ── get_dns_query_rate ────────────────────────────────────────────────


class GetDNSQueryRateArgs(BaseModel):
    server_id: str | None = Field(
        default=None,
        description="Optional UUID of a specific DNS server. Default sums across all servers.",
    )
    since_hours: float = Field(default=24, ge=0.5, le=168, description="Window in hours.")
    bucket_minutes: Literal[5, 15, 60] = Field(
        default=60,
        description=(
            "Bucket size for aggregation. 5 / 15 / 60 — match the "
            "underlying ``dns_metric_sample`` resolution. Picking a "
            "coarser bucket reduces the row count returned."
        ),
    )
    limit: int = Field(default=24, ge=1, le=200)


@register_tool(
    name="get_dns_query_rate",
    description=(
        "DNS query-rate timeseries from the ``dns_metric_sample`` "
        "table. Returns recent buckets with queries_total / noerror "
        "/ nxdomain / servfail / recursion counts. Use for 'are DNS "
        "queries spiking?' or 'show NXDOMAIN trend for the last "
        "12h'. Capped to 24 buckets by default."
    ),
    args_model=GetDNSQueryRateArgs,
    category="ops",
)
async def get_dns_query_rate(
    db: AsyncSession, user: User, args: GetDNSQueryRateArgs
) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(hours=args.since_hours)
    stmt = (
        select(
            DNSMetricSample.bucket_at,
            func.sum(DNSMetricSample.queries_total).label("queries_total"),
            func.sum(DNSMetricSample.noerror).label("noerror"),
            func.sum(DNSMetricSample.nxdomain).label("nxdomain"),
            func.sum(DNSMetricSample.servfail).label("servfail"),
            func.sum(DNSMetricSample.recursion).label("recursion"),
        )
        .where(DNSMetricSample.bucket_at >= cutoff)
        .group_by(DNSMetricSample.bucket_at)
        .order_by(desc(DNSMetricSample.bucket_at))
        .limit(args.limit)
    )
    if args.server_id:
        stmt = stmt.where(DNSMetricSample.server_id == args.server_id)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "bucket_at": r.bucket_at.isoformat() if r.bucket_at else None,
            "queries_total": int(r.queries_total or 0),
            "noerror": int(r.noerror or 0),
            "nxdomain": int(r.nxdomain or 0),
            "servfail": int(r.servfail or 0),
            "recursion": int(r.recursion or 0),
        }
        for r in rows
    ]


# ── get_dhcp_lease_rate ───────────────────────────────────────────────


class GetDHCPLeaseRateArgs(BaseModel):
    server_id: str | None = Field(
        default=None,
        description="Optional UUID of a specific DHCP server. Default sums across all servers.",
    )
    since_hours: float = Field(default=24, ge=0.5, le=168, description="Window in hours.")
    limit: int = Field(default=24, ge=1, le=200)


@register_tool(
    name="get_dhcp_lease_rate",
    description=(
        "DHCP packet-rate timeseries from the ``dhcp_metric_sample`` "
        "table. Returns recent buckets with discover / offer / "
        "request / ack / nak / decline / release / inform counts. "
        "Use for 'are DHCP NAKs climbing?' or 'show lease activity "
        "for server X over 12h'. Capped to 24 buckets by default."
    ),
    args_model=GetDHCPLeaseRateArgs,
    category="ops",
)
async def get_dhcp_lease_rate(
    db: AsyncSession, user: User, args: GetDHCPLeaseRateArgs
) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(hours=args.since_hours)
    stmt = (
        select(
            DHCPMetricSample.bucket_at,
            func.sum(DHCPMetricSample.discover).label("discover"),
            func.sum(DHCPMetricSample.offer).label("offer"),
            func.sum(DHCPMetricSample.request).label("request"),
            func.sum(DHCPMetricSample.ack).label("ack"),
            func.sum(DHCPMetricSample.nak).label("nak"),
            func.sum(DHCPMetricSample.decline).label("decline"),
            func.sum(DHCPMetricSample.release).label("release"),
            func.sum(DHCPMetricSample.inform).label("inform"),
        )
        .where(DHCPMetricSample.bucket_at >= cutoff)
        .group_by(DHCPMetricSample.bucket_at)
        .order_by(desc(DHCPMetricSample.bucket_at))
        .limit(args.limit)
    )
    if args.server_id:
        stmt = stmt.where(DHCPMetricSample.server_id == args.server_id)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "bucket_at": r.bucket_at.isoformat() if r.bucket_at else None,
            "discover": int(r.discover or 0),
            "offer": int(r.offer or 0),
            "request": int(r.request or 0),
            "ack": int(r.ack or 0),
            "nak": int(r.nak or 0),
            "decline": int(r.decline or 0),
            "release": int(r.release or 0),
            "inform": int(r.inform or 0),
        }
        for r in rows
    ]


# ── global_search ─────────────────────────────────────────────────────
#
# Reuses the helpers behind ``GET /api/v1/search`` so the tool's
# response shape matches what the UI's Cmd-K palette returns. We
# import lazily inside the tool body to avoid pulling the search
# router's full FastAPI namespace on backend boot.


class GlobalSearchArgs(BaseModel):
    query: str = Field(
        description=(
            "Free-form search string. Accepts an IP address, CIDR, MAC, "
            "FQDN, hostname, name fragment, or any custom-field value."
        ),
        min_length=1,
        max_length=200,
    )
    types: list[str] | None = Field(
        default=None,
        description=(
            "Restrict to specific resource types. Allowed values: "
            "ip_address, subnet, block, space, dns_group, dns_zone, "
            "dns_record. Default returns matches across all types."
        ),
    )
    limit: int = Field(default=25, ge=1, le=100)


@register_tool(
    name="global_search",
    description=(
        "Cross-resource search across IPAM (IPs / subnets / blocks / "
        "spaces) and DNS (groups / zones / records). Same lookup the "
        "UI's Cmd-K palette runs. Use when the operator gives a "
        "free-form identifier (an IP, CIDR, MAC, FQDN, or a partial "
        "name) and you don't yet know which resource type they mean. "
        "Returns hits with breadcrumbs (subnet → block → space) so "
        "you can name the path to the row."
    ),
    args_model=GlobalSearchArgs,
    category="ops",
)
async def global_search(
    db: AsyncSession, user: User, args: GlobalSearchArgs
) -> list[dict[str, Any]]:
    # Lazy import — the router module pulls in FastAPI router glue on
    # import; we only need the helpers.
    from app.api.v1.search import router as search_router  # noqa: PLC0415

    requested = set(args.types) if args.types else None
    per_type = max(args.limit, 10)
    results: list[Any] = []

    if not requested or "ip_address" in requested:
        results.extend(await search_router._search_addresses(db, args.query, per_type))
    if not requested or "subnet" in requested:
        results.extend(await search_router._search_subnets(db, args.query, per_type))
    if not requested or "block" in requested:
        results.extend(await search_router._search_blocks(db, args.query, per_type))
    if not requested or "space" in requested:
        results.extend(await search_router._search_spaces(db, args.query, per_type))
    if not requested or "dns_group" in requested:
        results.extend(await search_router._search_dns_groups(db, args.query, per_type))
    if not requested or "dns_zone" in requested:
        results.extend(await search_router._search_dns_zones(db, args.query, per_type))
    if not requested or "dns_record" in requested:
        results.extend(await search_router._search_dns_records(db, args.query, per_type))
    if not requested or requested & {"ip_address", "subnet", "block"}:
        results.extend(await search_router._search_custom_fields(db, args.query, per_type))

    # Dedup by (type, id) — the same row can hit multiple passes.
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for r in results:
        key = (r.type, r.id)
        if key in seen:
            continue
        seen.add(key)
        out.append(r.model_dump())
        if len(out) >= args.limit:
            break
    return out


# Silence the false-positive "imported but unused" — ``or_`` is part
# of the standard import block we share with sibling tool modules.
_ = or_
