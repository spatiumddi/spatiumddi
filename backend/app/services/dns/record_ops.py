"""Enqueue and resolve RecordOps for DNS agents.

Per docs/deployment/DNS_AGENT.md §5: when a record is mutated, compute the
delta and write RecordOp rows targeting the primary server for that zone.
Secondaries pick up the changes via native AXFR/IXFR from the primary.

Agentless drivers (Windows DNS today) don't follow this queue — the control
plane applies the change directly at enqueue time and writes the row as
``applied`` / ``failed`` so operators still see a per-op audit trail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.dns import get_driver, is_agentless
from app.drivers.dns.base import RecordChange, RecordData
from app.models.dns import DNSRecordOp, DNSServer, DNSZone

logger = structlog.get_logger(__name__)


async def resolve_primary_server(db: AsyncSession, zone: DNSZone) -> DNSServer | None:
    """Find the `is_primary=True` server in the zone's group."""
    res = await db.execute(
        select(DNSServer)
        .where(DNSServer.group_id == zone.group_id, DNSServer.is_primary.is_(True))
        .limit(1)
    )
    return res.scalar_one_or_none()


async def _apply_agentless(
    db: AsyncSession,
    server: DNSServer,
    zone: DNSZone,
    op: str,
    record: dict[str, Any],
    target_serial: int | None,
) -> DNSRecordOp:
    """Apply a record op synchronously via the server's driver.

    Writes a DNSRecordOp row marked ``applied`` on success or ``failed`` on
    error. The request path continues either way — a failure is visible in
    the record-ops dashboard and via the existing IPAM↔DNS sync-check.
    """
    op_row = DNSRecordOp(
        server_id=server.id,
        zone_name=zone.name,
        op=op,
        record=record,
        target_serial=target_serial,
        state="pending",
    )
    db.add(op_row)
    await db.flush()

    change = RecordChange(
        op=op,  # type: ignore[arg-type]
        zone_name=zone.name,
        record=RecordData(
            name=record["name"],
            record_type=record["type"],
            value=record["value"],
            ttl=record.get("ttl"),
            priority=record.get("priority"),
            weight=record.get("weight"),
            port=record.get("port"),
        ),
        target_serial=target_serial or 0,
    )

    try:
        driver = get_driver(server.driver)
        await driver.apply_record_change(server, change)
        op_row.state = "applied"
        op_row.applied_at = datetime.now(UTC)
        op_row.attempts = 1
        op_row.last_error = None
        logger.info(
            "record_op_applied_agentless",
            server=str(server.id),
            driver=server.driver,
            zone=zone.name,
            op=op,
            name=record["name"],
            type=record["type"],
        )
    except Exception as exc:  # noqa: BLE001 — surface any wire / config error
        op_row.state = "failed"
        op_row.attempts = 1
        op_row.last_error = str(exc)[:500]
        logger.warning(
            "record_op_failed_agentless",
            server=str(server.id),
            driver=server.driver,
            zone=zone.name,
            op=op,
            error=str(exc),
        )

    await db.flush()
    return op_row


async def enqueue_record_op(
    db: AsyncSession,
    zone: DNSZone,
    op: str,
    record: dict[str, Any],
    target_serial: int | None = None,
) -> DNSRecordOp | None:
    """Queue a record operation against the primary for this zone.

    * Agent-based primaries (BIND9): write a ``pending`` row; the agent
      picks it up on its next long-poll and applies via loopback nsupdate.
    * Agentless primaries (Windows DNS): apply immediately via the driver
      from the control plane; the row lands as ``applied`` or ``failed``.

    Returns the created op, or None if no primary is configured (the caller
    should surface a "no primary" warning to the user in that case).
    """
    primary = await resolve_primary_server(db, zone)
    if primary is None:
        # Silent drop was a footgun: frontend got a 200, nothing landed. Log
        # it loudly so the symptom shows up in `docker compose logs -f api`
        # and in the audit log via the caller.
        logger.warning(
            "record_op_dropped_no_primary",
            zone=zone.name,
            group_id=str(zone.group_id),
            op=op,
            name=record.get("name"),
            type=record.get("type"),
            hint=(
                "No DNS server in this zone's group has is_primary=True. "
                "Edit the server in DNS → Server Groups and mark one as primary."
            ),
        )
        return None

    if is_agentless(primary.driver):
        return await _apply_agentless(db, primary, zone, op, record, target_serial)

    op_row = DNSRecordOp(
        server_id=primary.id,
        zone_name=zone.name,
        op=op,
        record=record,
        target_serial=target_serial,
        state="pending",
    )
    db.add(op_row)
    await db.flush()
    return op_row


async def ack_op(db: AsyncSession, op_id: str, result: str, message: str | None = None) -> None:
    """Mark an op applied (ok) or failed."""
    from datetime import UTC, datetime

    op = await db.get(DNSRecordOp, op_id)
    if op is None:
        return
    op.attempts += 1
    if result == "ok":
        op.state = "applied"
        op.applied_at = datetime.now(UTC)
        op.last_error = None
    else:
        op.last_error = message
        if op.attempts >= 5:
            op.state = "failed"
        else:
            # Reset to pending so it gets re-shipped in the next bundle.
            op.state = "pending"
    await db.flush()
