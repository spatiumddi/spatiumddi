"""Enqueue and resolve RecordOps for DNS agents.

Per docs/deployment/DNS_AGENT.md §5: when a record is mutated, compute the
delta and write RecordOp rows targeting the primary server for that zone.
Secondaries pick up the changes via native AXFR/IXFR from the primary.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns import DNSRecordOp, DNSServer, DNSZone


async def resolve_primary_server(db: AsyncSession, zone: DNSZone) -> DNSServer | None:
    """Find the `is_primary=True` server in the zone's group."""
    res = await db.execute(
        select(DNSServer)
        .where(DNSServer.group_id == zone.group_id, DNSServer.is_primary.is_(True))
        .limit(1)
    )
    return res.scalar_one_or_none()


async def enqueue_record_op(
    db: AsyncSession,
    zone: DNSZone,
    op: str,
    record: dict[str, Any],
    target_serial: int | None = None,
) -> DNSRecordOp | None:
    """Queue a record operation against the primary for this zone.

    Returns the created op, or None if no primary is configured (the caller
    should surface a "no primary" warning to the user in that case).
    """
    primary = await resolve_primary_server(db, zone)
    if primary is None:
        return None
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
