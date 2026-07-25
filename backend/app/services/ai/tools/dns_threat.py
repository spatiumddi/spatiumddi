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

import ipaddress
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.dns_threat import DNSClientWindow
from app.services.ai.tools.base import register_tool
from app.services.dns_threat.aggregate import INTERESTING_SCORE


class FindSuspiciousDNSClientsArgs(BaseModel):
    hours: int = Field(default=24, ge=1, le=24 * 30, description="Trailing window to consider.")
    min_score: float = Field(
        default=INTERESTING_SCORE,
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
    # Explicit per non-negotiable #13. Enabled: the surface exposes no
    # secrets and makes no off-prem call, and it is already behind a
    # default-off module that an operator had to turn on knowing it
    # reads query content — gating it twice would just hide the tool
    # from the person who opted in.
    default_enabled=True,
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
        # An LLM will cheerfully pass a hostname or a CIDR here; against
        # an INET column that is a 500 and an internal_error row rather
        # than an answer.
        try:
            ip = str(ipaddress.ip_address(args.client_ip.strip()))
        except ValueError:
            return [
                {
                    "error": (
                        f"client_ip must be a single IP address, got "
                        f"{args.client_ip!r}. Resolve the hostname first, or drop "
                        f"the filter to see the worst-scoring clients."
                    )
                }
            ]
        stmt = stmt.where(DNSClientWindow.client_ip == ip)
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
    default_enabled=True,
)
async def get_dns_threat_summary(
    db: AsyncSession, user: User, args: DNSThreatSummaryArgs
) -> dict[str, Any]:
    # Shared with the REST endpoint and the dashboard card so the
    # copilot can't answer against a different "suspicious" threshold
    # than the UI is showing.
    from app.services.dns_threat.aggregate import compute_threat_summary  # noqa: PLC0415

    out = await compute_threat_summary(db, hours=args.hours)
    out["since"] = out["since"].isoformat()
    if not out["has_data"]:
        out["note"] = (
            "No client windows have been scored in this period. The rollup "
            "requires the security.dns_threat module enabled and query "
            "logging on at least one DNS server group — an empty result "
            "here is NOT an all-clear."
        )
    return out
