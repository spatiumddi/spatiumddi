"""Alerts — rule evaluator + delivery.

Called once per minute from a Celery beat tick (see tasks/alerts.py).
For each enabled ``AlertRule`` we:

  1. Compute the set of subjects that currently match the rule.
  2. For each newly-matching subject with no existing open event,
     open a new ``AlertEvent`` and dispatch it to the configured
     delivery channels (syslog + webhook, reusing the platform-level
     audit-forward targets).
  3. For each open event whose subject no longer matches, flip
     ``resolved_at`` to now.

The filter from ``PlatformSettings.utilization_max_prefix_*`` applies
to ``subnet_utilization`` rules so small PTP / loopback subnets can't
trip the alarm — same predicate the dashboard honours.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertEvent, AlertRule
from app.models.dhcp import DHCPServer
from app.models.dns import DNSServer
from app.models.ipam import Subnet
from app.models.settings import PlatformSettings
from app.services import audit_forward

logger = structlog.get_logger(__name__)


RULE_TYPE_SUBNET_UTILIZATION = "subnet_utilization"
RULE_TYPE_SERVER_UNREACHABLE = "server_unreachable"
RULE_TYPES = frozenset({RULE_TYPE_SUBNET_UTILIZATION, RULE_TYPE_SERVER_UNREACHABLE})


def _prefix_len(network: str) -> tuple[int, int] | None:
    """Return (prefix_len, family) — family is 4 or 6. None on parse error."""
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError:
        return None
    return net.prefixlen, net.version


def _include_subnet(subnet: Subnet, settings: PlatformSettings | None) -> bool:
    """Mirror of frontend/src/lib/utilization.ts:includeInUtilization."""
    if settings is None:
        return True
    parsed = _prefix_len(str(subnet.network))
    if parsed is None:
        return True
    prefix, family = parsed
    max_prefix = (
        settings.utilization_max_prefix_ipv4
        if family == 4
        else settings.utilization_max_prefix_ipv6
    )
    return prefix <= max_prefix


# ── Subject evaluation ─────────────────────────────────────────────────────


async def _matching_subnet_subjects(
    db: AsyncSession,
    rule: AlertRule,
    settings: PlatformSettings | None,
) -> list[tuple[str, str, str]]:
    """Return [(subject_id, display, message), ...] for a subnet_utilization rule."""
    threshold = rule.threshold_percent if rule.threshold_percent is not None else 90
    res = await db.execute(select(Subnet).where(Subnet.utilization_percent >= threshold))
    subnets = list(res.scalars().all())
    matches: list[tuple[str, str, str]] = []
    for s in subnets:
        if not _include_subnet(s, settings):
            continue
        pct = float(s.utilization_percent)
        display = f"{s.network}" + (f" — {s.name}" if s.name else "")
        message = (
            f"Subnet {display} utilisation {pct:.1f}% (threshold {threshold}%) — "
            f"{s.allocated_ips}/{s.total_ips} IPs allocated"
        )
        matches.append((str(s.id), display, message))
    return matches


async def _matching_server_subjects(
    db: AsyncSession, rule: AlertRule
) -> list[tuple[str, str, str]]:
    """Return matches for a server_unreachable rule."""
    server_type = rule.server_type or "any"
    matches: list[tuple[str, str, str]] = []

    if server_type in ("dns", "any"):
        res = await db.execute(
            select(DNSServer).where(
                or_(DNSServer.status == "unreachable", DNSServer.status == "error")
            )
        )
        for s in res.scalars().all():
            display = f"DNS {s.name}"
            message = f"DNS server {s.name} is {s.status}"
            matches.append((f"dns:{s.id}", display, message))

    if server_type in ("dhcp", "any"):
        res = await db.execute(
            select(DHCPServer).where(
                or_(DHCPServer.status == "unreachable", DHCPServer.status == "error")
            )
        )
        for s in res.scalars().all():
            display = f"DHCP {s.name}"
            message = f"DHCP server {s.name} is {s.status}"
            matches.append((f"dhcp:{s.id}", display, message))

    return matches


# ── Delivery ───────────────────────────────────────────────────────────────


def _severity_to_syslog(severity: str) -> int:
    """Map alert severity → RFC 5424 severity (mirrors audit_forward)."""
    if severity == "critical":
        return 2  # crit
    if severity == "warning":
        return 4  # warning
    return 6  # info


async def _deliver(
    rule: AlertRule,
    event: AlertEvent,
    syslog_cfg: dict[str, Any] | None,
    webhook_cfg: dict[str, Any] | None,
) -> tuple[bool, bool]:
    """Push one newly-opened event out to the configured channels.

    Returns (delivered_syslog, delivered_webhook) as booleans suitable
    for stamping onto the event row. Failures are logged via structlog
    and not raised — a dead collector never blocks alert creation.
    """
    delivered_syslog = False
    delivered_webhook = False

    payload: dict[str, Any] = {
        "kind": "alert",
        "rule_id": str(rule.id),
        "rule_name": rule.name,
        "rule_type": rule.rule_type,
        "severity": event.severity,
        "fired_at": event.fired_at.isoformat(),
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "subject_display": event.subject_display,
        "message": event.message,
    }

    if rule.notify_syslog and syslog_cfg is not None:
        facility = int(syslog_cfg["facility"])
        severity = _severity_to_syslog(event.severity)
        pri = (facility << 3) | severity
        hostname = audit_forward._hostname()  # noqa: SLF001
        ts = event.fired_at.isoformat()
        # Matches the RFC 5424 shape audit_forward renders — JSON body
        # after the header.
        import json  # noqa: PLC0415

        msg = f"<{pri}>1 {ts} {hostname} spatiumddi - ALERT - " + json.dumps(
            payload, separators=(",", ":"), default=str
        )
        try:
            await audit_forward._send_syslog(  # noqa: SLF001
                syslog_cfg["host"],
                int(syslog_cfg["port"]),
                syslog_cfg["protocol"],
                msg,
            )
            delivered_syslog = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "alert_deliver_syslog_failed",
                rule=str(rule.id),
                event=str(event.id),
                error=str(exc),
            )

    if rule.notify_webhook and webhook_cfg is not None:
        try:
            await audit_forward._send_webhook(  # noqa: SLF001
                webhook_cfg["url"],
                webhook_cfg["auth_header"],
                payload,
            )
            delivered_webhook = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "alert_deliver_webhook_failed",
                rule=str(rule.id),
                event=str(event.id),
                error=str(exc),
            )

    return delivered_syslog, delivered_webhook


# ── Main entry point ───────────────────────────────────────────────────────


async def evaluate_all(db: AsyncSession) -> dict[str, int]:
    """Evaluate every enabled rule; open / resolve events as needed.

    Returns a summary dict for the scheduled-task audit row: opened,
    resolved, delivered_syslog, delivered_webhook. Per-rule failures are
    logged but don't abort the pass — one broken rule shouldn't silence
    the rest.
    """
    settings = await db.get(PlatformSettings, 1)
    syslog_cfg, webhook_cfg = await audit_forward._load_forward_config()  # noqa: SLF001

    # Alerts have their own enabled toggle per rule; we still rely on
    # audit-forward's target config for actual delivery. If the user
    # hasn't configured either target the event is recorded but goes
    # nowhere — still visible in the /alerts UI.
    now = datetime.now(UTC)

    opened = 0
    resolved = 0
    delivered_syslog = 0
    delivered_webhook = 0

    res = await db.execute(select(AlertRule).where(AlertRule.enabled.is_(True)))
    rules = list(res.scalars().all())
    for rule in rules:
        try:
            if rule.rule_type == RULE_TYPE_SUBNET_UTILIZATION:
                matches = await _matching_subnet_subjects(db, rule, settings)
                subject_type = "subnet"
            elif rule.rule_type == RULE_TYPE_SERVER_UNREACHABLE:
                matches = await _matching_server_subjects(db, rule)
                subject_type = "server"
            else:
                logger.warning("alert_unknown_rule_type", rule=str(rule.id), type=rule.rule_type)
                continue

            # Index current open events by subject_id for this rule.
            open_res = await db.execute(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id,
                    AlertEvent.resolved_at.is_(None),
                )
            )
            open_events = list(open_res.scalars().all())
            open_by_subject = {ev.subject_id: ev for ev in open_events}

            match_ids = {sid for sid, _, _ in matches}

            # Open new events for unseen matches.
            for subject_id, display, message in matches:
                if subject_id in open_by_subject:
                    continue
                event = AlertEvent(
                    rule_id=rule.id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    subject_display=display,
                    severity=rule.severity,
                    message=message,
                    fired_at=now,
                )
                db.add(event)
                await db.flush()  # populate event.id for delivery payload
                ds, dw = await _deliver(rule, event, syslog_cfg, webhook_cfg)
                event.delivered_syslog = ds
                event.delivered_webhook = dw
                opened += 1
                if ds:
                    delivered_syslog += 1
                if dw:
                    delivered_webhook += 1

            # Resolve open events whose subject no longer matches.
            for subject_id, event in open_by_subject.items():
                if subject_id in match_ids:
                    continue
                event.resolved_at = now
                resolved += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "alert_rule_eval_failed",
                rule=str(rule.id),
                rule_type=rule.rule_type,
                error=str(exc),
            )

    await db.commit()
    return {
        "opened": opened,
        "resolved": resolved,
        "delivered_syslog": delivered_syslog,
        "delivered_webhook": delivered_webhook,
    }
