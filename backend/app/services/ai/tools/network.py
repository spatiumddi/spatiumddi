"""Read-only network / ops tools for the Operator Copilot
(issue #90 Wave 2). Covers SNMP devices, alerts, audit log.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertEvent, AlertRule
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.network import NetworkDevice
from app.services.ai.tools.base import register_tool

# ── network devices ───────────────────────────────────────────────────


class ListDevicesArgs(BaseModel):
    device_type: str | None = Field(
        default=None,
        description="Filter by device_type: router / switch / ap / firewall / l3_switch / other.",
    )
    space_id: str | None = Field(
        default=None,
        description="Filter by IPSpace UUID — only devices bound to this space.",
    )
    search: str | None = Field(
        default=None,
        description="Substring match on device name, hostname, or sys_descr.",
    )
    limit: int = Field(default=100, ge=1, le=500)


@register_tool(
    name="list_network_devices",
    description=(
        "List SNMP-polled network devices (routers / switches / APs / "
        "firewalls). Each summary includes name, IP, device type, "
        "vendor, sys_descr, last successful poll, and the IP space "
        "it's bound to."
    ),
    args_model=ListDevicesArgs,
    category="network",
)
async def list_network_devices(
    db: AsyncSession, user: User, args: ListDevicesArgs
) -> list[dict[str, Any]]:
    stmt = select(NetworkDevice)
    if args.device_type:
        stmt = stmt.where(NetworkDevice.device_type == args.device_type)
    if args.space_id:
        stmt = stmt.where(NetworkDevice.space_id == args.space_id)
    if args.search:
        like = f"%{args.search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(NetworkDevice.name).like(like),
                func.lower(NetworkDevice.hostname).like(like),
                func.lower(NetworkDevice.sys_descr).like(like),
            )
        )
    stmt = stmt.order_by(NetworkDevice.name.asc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "hostname": d.hostname,
            "ip_address": str(d.ip_address),
            "device_type": d.device_type,
            "vendor": d.vendor,
            "sys_descr": d.sys_descr,
            "sys_uptime_seconds": d.sys_uptime_seconds,
            "last_polled_at": d.last_polled_at.isoformat() if d.last_polled_at else None,
            "last_poll_status": d.last_poll_status,
        }
        for d in rows
    ]


# ── alerts ────────────────────────────────────────────────────────────


class ListAlertsArgs(BaseModel):
    severity: str | None = Field(
        default=None,
        description="Filter by severity: info / warning / critical.",
    )
    open_only: bool = Field(
        default=True,
        description="If True (default), only currently-open alerts (resolved_at IS NULL).",
    )
    subject_type: str | None = Field(
        default=None,
        description="Filter by subject type — 'subnet' or 'server'.",
    )
    limit: int = Field(default=50, ge=1, le=200)


@register_tool(
    name="list_alerts",
    description=(
        "List alert events. Defaults to open (unresolved) alerts; "
        "set ``open_only=False`` for historical alerts. Each event "
        "includes severity, subject, message, fired / resolved "
        "timestamps."
    ),
    args_model=ListAlertsArgs,
    category="ops",
)
async def list_alerts(db: AsyncSession, user: User, args: ListAlertsArgs) -> list[dict[str, Any]]:
    stmt = select(AlertEvent)
    if args.open_only:
        stmt = stmt.where(AlertEvent.resolved_at.is_(None))
    if args.severity:
        stmt = stmt.where(AlertEvent.severity == args.severity)
    if args.subject_type:
        stmt = stmt.where(AlertEvent.subject_type == args.subject_type)
    stmt = stmt.order_by(AlertEvent.fired_at.desc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(e.id),
            "rule_id": str(e.rule_id),
            "subject_type": e.subject_type,
            "subject_id": e.subject_id,
            "subject_display": e.subject_display,
            "severity": e.severity,
            "message": e.message,
            "fired_at": e.fired_at.isoformat() if e.fired_at else None,
            "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
        }
        for e in rows
    ]


class ListAlertRulesArgs(BaseModel):
    enabled_only: bool = True


@register_tool(
    name="list_alert_rules",
    description=(
        "List configured alert rules. Each summary includes name, "
        "rule type, enabled flag, and key thresholds."
    ),
    args_model=ListAlertRulesArgs,
    category="ops",
)
async def list_alert_rules(
    db: AsyncSession, user: User, args: ListAlertRulesArgs
) -> list[dict[str, Any]]:
    stmt = select(AlertRule)
    if args.enabled_only:
        stmt = stmt.where(AlertRule.enabled.is_(True))
    stmt = stmt.order_by(AlertRule.name.asc())
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "rule_type": r.rule_type,
            "enabled": r.enabled,
            "severity": r.severity,
        }
        for r in rows
    ]


# ── audit ─────────────────────────────────────────────────────────────


class GetAuditHistoryArgs(BaseModel):
    user_id: str | None = Field(default=None, description="Filter by acting user UUID.")
    resource_type: str | None = Field(
        default=None,
        description="Filter by resource type — e.g. 'subnet', 'dns_zone', 'ai_provider'.",
    )
    resource_id: str | None = Field(
        default=None,
        description="Filter by exact resource UUID. Use to retrieve the change history of one row.",
    )
    action: str | None = Field(
        default=None,
        description="Filter by action — create / update / delete / login / logout / etc.",
    )
    limit: int = Field(default=50, ge=1, le=500)


@register_tool(
    name="get_audit_history",
    description=(
        "Query the append-only audit log. Filters: user, resource "
        "type, resource id, action. Returns chronological audit "
        "entries with old / new values when set. Useful for 'who "
        "changed X?' and 'show me recent activity by user Y'."
    ),
    args_model=GetAuditHistoryArgs,
    category="ops",
)
async def get_audit_history(
    db: AsyncSession, user: User, args: GetAuditHistoryArgs
) -> list[dict[str, Any]]:
    stmt = select(AuditLog)
    if args.user_id:
        stmt = stmt.where(AuditLog.user_id == args.user_id)
    if args.resource_type:
        stmt = stmt.where(AuditLog.resource_type == args.resource_type)
    if args.resource_id:
        stmt = stmt.where(AuditLog.resource_id == args.resource_id)
    if args.action:
        stmt = stmt.where(AuditLog.action == args.action)
    stmt = stmt.order_by(AuditLog.timestamp.desc()).limit(args.limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "user_id": str(r.user_id) if r.user_id else None,
            "user_display_name": r.user_display_name,
            "auth_source": r.auth_source,
            "action": r.action,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "resource_display": r.resource_display,
            "result": r.result,
            "changed_fields": r.changed_fields,
        }
        for r in rows
    ]
