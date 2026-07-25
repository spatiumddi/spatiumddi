"""DNS threat analytics tools for the Operator Copilot (issue #699).

Two read-only tools over the per-client tunneling rollup. No
``propose_*`` writes: there is nothing to mutate here — the rollup
writes itself, and the operator action a finding calls for (isolate the
host, pull a packet capture, check what it's running) lives behind
other tools with their own gates.

Not superadmin-gated, unlike the backup tools: this is a summary of the
DNS query log, and the permission model already decides who may read
that. It is tagged to the ``security.dns_threat`` module so the tools
disappear when the feature is off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.dns_threat import DNSClientWindow
from app.services.ai.tools.base import register_tool


class FindSuspiciousDNSClientsArgs(BaseModel):
    hours: int = Field(default=24, ge=1, le=24 * 30, description="Trailing window to consider.")
    min_score: float = Field(
        default=20.0,
        ge=0,
        le=100,
        description=(
            "Minimum tunneling score (0-100). 60+ is a strong signal; 20-60 is "
            "worth a look but is often busy-but-benign traffic."
        ),
    )
    client_ip: str | None = Field(
        default=None,
        description="Restrict to one client IP — use for 'why did 10.0.0.5 get flagged?'.",
    )
    limit: int = Field(default=25, ge=1, le=200)


@register_tool(
    name="find_suspicious_dns_clients",
    description=(
        "Find DNS clients whose query behaviour scores as tunneling / "
        "exfiltration (issue #699). DNS tunneling hides payload in the "
        "names a host looks up, under a domain the attacker controls — "
        "it is the exfil path firewalls do not inspect. Each row "
        "carries the client IP, the 0-100 score, the domain the traffic "
        "concentrated on, and the individual signals with their "
        "contributions (label length, label entropy, subdomain fan-out, "
        "payload-qtype ratio) so the answer explains itself. Use for "
        "'is anything tunnelling over DNS?', 'why was 10.0.0.5 "
        "flagged?', or 'what is the worst DNS client this week?'. "
        "Windows whose traffic was entirely to allowlisted domains "
        "(DNSBL / CDN / reverse-DNS, which look like tunnels by design) "
        "are excluded."
    ),
    args_model=FindSuspiciousDNSClientsArgs,
    category="dns",
    module="security.dns_threat",
)
async def find_suspicious_dns_clients(
    db: AsyncSession, user: User, args: FindSuspiciousDNSClientsArgs
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(hours=args.hours)
    stmt = (
        select(DNSClientWindow)
        .where(
            DNSClientWindow.window_start >= since,
            DNSClientWindow.allowlisted.is_(False),
        )
        .order_by(desc(DNSClientWindow.tunnel_score), desc(DNSClientWindow.window_start))
        .limit(args.limit)
    )
    if args.client_ip:
        stmt = stmt.where(DNSClientWindow.client_ip == args.client_ip)
    else:
        stmt = stmt.where(DNSClientWindow.tunnel_score >= args.min_score)
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return [
            {
                "result": "no matching client windows",
                "note": (
                    "Either nothing is scoring above the threshold, or the rollup "
                    "has no data — the security.dns_threat module must be enabled "
                    "AND a DNS server group must have query logging turned on."
                ),
            }
        ]
    return [
        {
            "client_ip": str(w.client_ip),
            "tunnel_score": round(w.tunnel_score, 1),
            "window_start": w.window_start.isoformat(),
            "window_end": w.window_end.isoformat(),
            "query_count": w.query_count,
            "distinct_qnames": w.distinct_qnames,
            "top_parent": w.top_parent,
            "top_parent_subdomains": w.top_parent_subdomains,
            "max_label_length": w.max_label_length,
            "mean_label_entropy": round(w.mean_label_entropy, 2),
            "payload_qtype_count": w.payload_qtype_count,
            "signals": w.tunnel_signals or [],
        }
        for w in rows
    ]


class DNSThreatSummaryArgs(BaseModel):
    hours: int = Field(default=24, ge=1, le=24 * 30)


@register_tool(
    name="get_dns_threat_summary",
    description=(
        "One-shot rollup of DNS tunneling analytics over a trailing "
        "window (issue #699): how many client windows were scored, how "
        "many distinct clients, how many crossed the suspicious "
        "threshold, and the worst offender. Answers 'is anything "
        "exfiltrating over DNS right now?' without listing every "
        "client. Reports explicitly when the rollup has no data at all, "
        "so an idle result is never mistaken for an all-clear."
    ),
    args_model=DNSThreatSummaryArgs,
    category="dns",
    module="security.dns_threat",
)
async def get_dns_threat_summary(
    db: AsyncSession, user: User, args: DNSThreatSummaryArgs
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=args.hours)
    totals = (
        await db.execute(
            select(
                func.count().label("windows"),
                func.count(func.distinct(DNSClientWindow.client_ip)).label("clients"),
                func.coalesce(func.max(DNSClientWindow.tunnel_score), 0.0).label("peak"),
            ).where(DNSClientWindow.window_start >= since)
        )
    ).one()
    if not totals.windows:
        return {
            "has_data": False,
            "note": (
                "No client windows have been scored in this period. The rollup "
                "requires the security.dns_threat module enabled and query "
                "logging on at least one DNS server group — an empty result "
                "here is NOT an all-clear."
            ),
            "since": since.isoformat(),
        }
    worst = (
        await db.execute(
            select(DNSClientWindow)
            .where(
                DNSClientWindow.window_start >= since,
                DNSClientWindow.allowlisted.is_(False),
            )
            .order_by(desc(DNSClientWindow.tunnel_score))
            .limit(1)
        )
    ).scalar_one_or_none()
    suspicious = (
        await db.execute(
            select(func.count(func.distinct(DNSClientWindow.client_ip))).where(
                DNSClientWindow.window_start >= since,
                DNSClientWindow.allowlisted.is_(False),
                DNSClientWindow.tunnel_score >= 20.0,
            )
        )
    ).scalar_one()
    return {
        "has_data": True,
        "since": since.isoformat(),
        "windows_scored": totals.windows,
        "clients_seen": totals.clients,
        "suspicious_clients": suspicious or 0,
        "peak_score": round(float(totals.peak or 0.0), 1),
        "worst_client_ip": str(worst.client_ip) if worst else None,
        "worst_client_score": round(worst.tunnel_score, 1) if worst else None,
        "worst_client_parent": worst.top_parent if worst else None,
    }
