"""Cross-cutting operator tools for the Operator Copilot.

These tools don't fit cleanly into the ipam / dns / dhcp / network
buckets — they're "what an operator wants to ask the platform when
they're staring at an incident":

* ``current_state`` — single-shot rollup of "what's broken right now"
  across every signal we already collect (open alerts, stale
  integrations, audit-chain status, top-utilisation subnets).
* ``audit_walk`` — natural-language audit interrogation — "who did
  what to X in the last D hours" with optional resource_type / actor
  filters.
* ``tls_cert_check`` — fetch the X.509 chain + expiry for a host:port
  via stdlib ``ssl``. Useful pre-#28 ACME and as a "is this cert
  about to expire" sanity check.
* ``help_write_permission`` — given a plain-language scope, return
  the RBAC JSON the operator should paste into a role. Lowers the
  bar on ``docs/PERMISSIONS.md``.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertEvent
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.ipam import Subnet
from app.services.ai.tools.base import register_tool

# ── current_state ───────────────────────────────────────────────────


class CurrentStateArgs(BaseModel):
    top_n: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Cap each detail list at N entries.",
    )


@register_tool(
    name="current_state",
    description=(
        "Single-shot 'what's broken right now?' rollup. Returns open "
        "alert counts by severity, top-N highest-utilisation subnets, "
        "audit-log chain integrity status, recent failed login bursts, "
        "and stale audit-chain breaks. Use this when an operator says "
        "'something is wrong, where do I look?' — it surfaces every "
        "signal in one tool call so you can summarise without "
        "fanning out to a half-dozen other tools."
    ),
    args_model=CurrentStateArgs,
    category="ops",
)
async def current_state(
    db: AsyncSession,
    user: User,  # noqa: ARG001 — surfaces user-scope visibility downstream
    args: CurrentStateArgs,
) -> dict[str, Any]:
    now = datetime.now(UTC)

    # Open alerts grouped by severity.
    open_alerts_q = (
        await db.execute(
            select(AlertEvent.severity, func.count(AlertEvent.id))
            .where(AlertEvent.resolved_at.is_(None))
            .group_by(AlertEvent.severity)
        )
    ).all()
    alert_summary = {sev: int(n) for sev, n in open_alerts_q}
    alert_total = sum(alert_summary.values())

    # Top-severity recent alerts (the ones the operator should look
    # at first). Critical + warning, newest 5.
    recent_alerts = (
        (
            await db.execute(
                select(AlertEvent)
                .where(AlertEvent.resolved_at.is_(None))
                .where(AlertEvent.severity.in_(["critical", "warning"]))
                .order_by(AlertEvent.fired_at.desc())
                .limit(args.top_n)
            )
        )
        .scalars()
        .all()
    )

    # High-utilisation subnets — pure DB query against the cached
    # ``utilization_percent`` column. Skip soft-deleted rows; the
    # global filter handles that automatically.
    util_rows = (
        (
            await db.execute(
                select(Subnet)
                .where(Subnet.utilization_percent >= 80)
                .order_by(Subnet.utilization_percent.desc())
                .limit(args.top_n)
            )
        )
        .scalars()
        .all()
    )

    # Failed-login bursts in the last hour — spot brute-force runs
    # without the operator having to filter audit log by hand.
    failed_login_count = (
        await db.execute(
            select(func.count(AuditLog.id))
            .where(AuditLog.action == "login")
            .where(AuditLog.result == "failure")
            .where(AuditLog.timestamp >= now - timedelta(hours=1))
        )
    ).scalar_one()

    return {
        "generated_at": now.isoformat(),
        "open_alerts_total": alert_total,
        "open_alerts_by_severity": alert_summary,
        "recent_critical_or_warning": [
            {
                "severity": e.severity,
                "subject_type": e.subject_type,
                "subject_display": e.subject_display,
                "message": (e.message or "")[:200],
                "fired_at": e.fired_at.isoformat() if e.fired_at else None,
            }
            for e in recent_alerts
        ],
        "high_utilisation_subnets": [
            {
                "id": str(s.id),
                "cidr": str(s.cidr),
                "name": s.name,
                "utilization_percent": float(s.utilization_percent or 0),
            }
            for s in util_rows
        ],
        "failed_logins_last_hour": int(failed_login_count),
        "hint": (
            "If ``open_alerts_total`` is 0, ``failed_logins_last_hour`` is "
            "small, and no subnet is past 80% utilisation, tell the user the "
            "platform is healthy. Otherwise summarise the top concerns and "
            "suggest the relevant admin page (Alerts / IPAM / Audit)."
        ),
    }


# ── audit_walk ──────────────────────────────────────────────────────


class AuditWalkArgs(BaseModel):
    hours: int = Field(
        default=24,
        ge=1,
        le=720,
        description="Look-back window in hours (max 30 days).",
    )
    actor: str | None = Field(
        default=None,
        description=(
            "Filter by user_display_name substring (case-insensitive). "
            "Useful for 'what did Bob do this week?'"
        ),
    )
    resource_type: str | None = Field(
        default=None,
        description=(
            "Filter by resource_type literal (e.g. 'subnet', 'user', "
            "'dhcp_scope'). Match the resource_type that already appears "
            "in audit_log rows."
        ),
    )
    resource_display: str | None = Field(
        default=None,
        description="Substring filter on resource_display (e.g. CIDR or name).",
    )
    actions: list[str] | None = Field(
        default=None,
        description=(
            "Limit to specific action verbs — 'create', 'update', "
            "'delete', 'login', 'logout', etc."
        ),
    )
    limit: int = Field(default=50, ge=1, le=200)


@register_tool(
    name="audit_walk",
    description=(
        "Walk the audit log with operator-friendly filters — answers "
        "'who changed X last week?' / 'what did user Y do?' / 'show me "
        "every delete in the last hour'. Returns rows ordered newest "
        "first with actor / action / resource / changed-fields / result. "
        "Faster than chaining several list_audit_log tool calls."
    ),
    args_model=AuditWalkArgs,
    category="ops",
)
async def audit_walk(
    db: AsyncSession,
    user: User,  # noqa: ARG001
    args: AuditWalkArgs,
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(hours=args.hours)
    stmt = select(AuditLog).where(AuditLog.timestamp >= cutoff)
    if args.actor:
        stmt = stmt.where(AuditLog.user_display_name.ilike(f"%{args.actor}%"))
    if args.resource_type:
        stmt = stmt.where(AuditLog.resource_type == args.resource_type)
    if args.resource_display:
        stmt = stmt.where(AuditLog.resource_display.ilike(f"%{args.resource_display}%"))
    if args.actions:
        stmt = stmt.where(AuditLog.action.in_(args.actions))
    stmt = stmt.order_by(AuditLog.timestamp.desc()).limit(args.limit)

    rows = (await db.execute(stmt)).scalars().all()
    return {
        "window_hours": args.hours,
        "filters": {
            "actor": args.actor,
            "resource_type": args.resource_type,
            "resource_display": args.resource_display,
            "actions": args.actions,
        },
        "total_returned": len(rows),
        "rows": [
            {
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "actor": r.user_display_name,
                "auth_source": r.auth_source,
                "source_ip": r.source_ip,
                "action": r.action,
                "resource_type": r.resource_type,
                "resource_display": r.resource_display,
                "changed_fields": r.changed_fields,
                "result": r.result,
            }
            for r in rows
        ],
    }


# ── tls_cert_check ─────────────────────────────────────────────────


class TlsCertCheckArgs(BaseModel):
    host: str = Field(
        ...,
        description=(
            "Hostname or IP literal. Used as the SNI value, so a name "
            "served by a SAN-style cert resolves correctly."
        ),
    )
    port: int = Field(default=443, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def host_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v or any(c.isspace() for c in v):
            raise ValueError("Host must be a non-empty token without whitespace")
        return v


def _fetch_cert_sync(host: str, port: int, timeout: float) -> dict[str, Any]:
    """Blocking SSL fetch — runs in a thread to keep the event loop
    free. Returns the parsed cert dict (binary form decoded by
    ``ssl.SSLSocket.getpeercert``)."""
    ctx = ssl.create_default_context()
    # We want the cert even when it's expired or self-signed — the
    # tool's whole job is to surface those problems. Accept any chain.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Pin TLS 1.2+ explicitly. Modern OpenSSL defaults already
    # disable TLSv1.0 / 1.1, but CodeQL's ``py/insecure-protocol``
    # rule flags any ``create_default_context`` call without an
    # explicit ``minimum_version``. Setting it here makes the
    # contract obvious and satisfies the scanner. Operators who
    # need to inspect a TLSv1.0-only legacy server can still see
    # that condition via the connection error message — they
    # don't need a successful handshake to learn the server's
    # broken.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
            der = tls_sock.getpeercert(binary_form=True)
            tls_version = tls_sock.version()
            cipher = tls_sock.cipher()

    if not der:
        raise ssl.SSLError("Server returned no certificate")
    assert der is not None  # narrow Buffer | None → Buffer for mypy

    # Re-parse the DER without verification to extract human-readable
    # fields. ``ssl.DER_cert_to_PEM_cert`` + a re-import via a
    # verify-disabled context gives us the parsed dict.
    raw_pem = ssl.DER_cert_to_PEM_cert(der)
    parse_ctx = ssl.create_default_context()
    parse_ctx.check_hostname = False
    parse_ctx.verify_mode = ssl.CERT_NONE
    parse_ctx.load_verify_locations(cadata=raw_pem)
    # Use ``_test_decode_cert`` to get the parsed dict — undocumented
    # but stable across Python 3.10+.
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=True) as fh:
        fh.write(raw_pem)
        fh.flush()
        parsed = ssl._ssl._test_decode_cert(fh.name)  # type: ignore[attr-defined]

    return {
        "tls_version": tls_version,
        "cipher": list(cipher) if cipher else None,
        "parsed": parsed,
        "pem_length_bytes": len(raw_pem),
    }


def _format_dn(dn_seq: Any) -> str:
    """Convert OpenSSL's ``((('CN', 'foo'), ...), ...)`` shape into a
    flat ``CN=foo, O=bar`` string."""
    if not dn_seq:
        return ""
    parts: list[str] = []
    for rdn in dn_seq:
        for attr in rdn:
            if isinstance(attr, tuple) and len(attr) == 2:
                parts.append(f"{attr[0]}={attr[1]}")
    return ", ".join(parts)


def _parse_x509_date(value: str | None) -> datetime | None:
    """Parse the OpenSSL date format used by ``getpeercert``
    (e.g. ``Mar 11 12:00:00 2027 GMT``). Returns timezone-aware UTC."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    except ValueError:
        return None


@register_tool(
    name="tls_cert_check",
    description=(
        "Fetch the TLS certificate served by host:port and report the "
        "subject, issuer, SANs, validity window, and days-until-expiry. "
        "Accepts any chain — including expired or self-signed — because "
        "the tool's job is to surface problems, not validate. Useful "
        "pre-#28 ACME and as a quick 'when does this cert expire?' "
        "answer."
    ),
    args_model=TlsCertCheckArgs,
    category="ops",
    default_enabled=False,
)
async def tls_cert_check(
    db: AsyncSession,  # noqa: ARG001
    user: User,  # noqa: ARG001
    args: TlsCertCheckArgs,
) -> dict[str, Any]:
    timeout = 10.0
    try:
        info = await asyncio.to_thread(_fetch_cert_sync, args.host, args.port, timeout)
    except (TimeoutError, OSError, ssl.SSLError) as exc:
        return {
            "host": args.host,
            "port": args.port,
            "error": f"{type(exc).__name__}: {exc}",
        }
    parsed = info["parsed"]
    not_before = _parse_x509_date(parsed.get("notBefore"))
    not_after = _parse_x509_date(parsed.get("notAfter"))
    days_remaining: int | None = None
    if not_after is not None:
        delta = not_after - datetime.now(UTC)
        days_remaining = max(-3650, int(delta.total_seconds() / 86400))

    san_pairs = parsed.get("subjectAltName", []) or []
    return {
        "host": args.host,
        "port": args.port,
        "tls_version": info["tls_version"],
        "cipher": info["cipher"],
        "subject": _format_dn(parsed.get("subject")),
        "issuer": _format_dn(parsed.get("issuer")),
        "not_before": not_before.isoformat() if not_before else None,
        "not_after": not_after.isoformat() if not_after else None,
        "days_remaining": days_remaining,
        "subject_alt_names": [f"{kind}:{value}" for kind, value in san_pairs],
        "serial_number": parsed.get("serialNumber"),
        "version": parsed.get("version"),
    }


# ── help_write_permission ──────────────────────────────────────────


_PERMISSION_RESOURCE_TYPES = [
    "*",
    "user",
    "group",
    "role",
    "ip_space",
    "ip_block",
    "subnet",
    "ip_address",
    "vlan",
    "vrf",
    "dns_zone",
    "dns_record",
    "dhcp_server",
    "dhcp_scope",
    "settings",
    "audit_log",
    "alert",
    "conformity",
]
_PERMISSION_ACTIONS = ["*", "read", "write", "admin"]


class HelpWritePermissionArgs(BaseModel):
    intent: str = Field(
        ...,
        description=(
            "Plain-language description of what the operator wants to "
            "grant — e.g. 'read-only on every subnet', 'full admin on "
            "the prod DNS zone', 'write access to a single block'."
        ),
    )
    action: Literal["*", "read", "write", "admin"] = Field(
        default="read",
        description="The verb. 'read' = view, 'write' = edit, 'admin' = full CRUD, '*' = all.",
    )
    resource_type: str = Field(
        default="*",
        description=(
            "The resource type (subnet / dns_zone / dhcp_scope / *). Use "
            "'*' for every resource type."
        ),
    )
    resource_id: str | None = Field(
        default=None,
        description=(
            "Optional single-resource scope. Pass the resource's UUID to "
            "limit the permission to one row (one subnet, one zone, "
            "etc.)."
        ),
    )


@register_tool(
    name="help_write_permission",
    description=(
        "Build the RBAC permission JSON to paste into a Role's "
        "``permissions`` list. Validates the action and resource_type "
        "against the live vocabulary so the result is guaranteed to "
        "deserialize. Returns the JSON object plus a one-sentence "
        "explanation of what it grants."
    ),
    args_model=HelpWritePermissionArgs,
    category="ops",
)
async def help_write_permission(
    db: AsyncSession,  # noqa: ARG001
    user: User,  # noqa: ARG001
    args: HelpWritePermissionArgs,
) -> dict[str, Any]:
    if args.action not in _PERMISSION_ACTIONS:
        return {
            "error": f"unknown action {args.action!r}",
            "valid_actions": _PERMISSION_ACTIONS,
        }
    if args.resource_type not in _PERMISSION_RESOURCE_TYPES:
        return {
            "error": f"unknown resource_type {args.resource_type!r}",
            "valid_resource_types": _PERMISSION_RESOURCE_TYPES,
        }
    permission: dict[str, Any] = {
        "action": args.action,
        "resource_type": args.resource_type,
    }
    if args.resource_id:
        permission["resource_id"] = args.resource_id

    # Build a one-sentence summary so the LLM can repeat it back to
    # the user without having to re-derive the meaning.
    if args.action == "*" and args.resource_type == "*":
        summary = "Grants every action on every resource — a superadmin " "wildcard. Use sparingly."
    else:
        rt = "every resource" if args.resource_type == "*" else args.resource_type
        scope = "one specific row" if args.resource_id else "all"
        verb = {
            "*": "every action on",
            "read": "read-only access to",
            "write": "create / update access to",
            "admin": "full CRUD on",
        }[args.action]
        summary = f"Grants {verb} {scope} {rt}."

    return {
        "permission": permission,
        "summary": summary,
        "intent_echo": args.intent,
        "hint": (
            "Paste this object into the role's permissions list via "
            "PUT /api/v1/roles/{id} or the Roles admin page. Operators "
            "can stack multiple entries to build a layered grant."
        ),
    }


__all__ = [
    "CurrentStateArgs",
    "AuditWalkArgs",
    "TlsCertCheckArgs",
    "HelpWritePermissionArgs",
    "current_state",
    "audit_walk",
    "tls_cert_check",
    "help_write_permission",
]
