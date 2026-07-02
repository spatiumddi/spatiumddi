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

from app.core.agent_wake import collect_wake, dns_group_channel
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
    """Queue a record operation against every applicable server in
    the zone's group.

    Driver semantics:

    * **Agentless** (Windows DNS): exactly one server in the group
      writes — the one marked ``is_primary=True``. Apply immediately
      via the driver from the control plane; the row lands as
      ``applied`` or ``failed``.
    * **Agent-based** (BIND9 / PowerDNS): every enabled, agent-based
      server in the group runs an independent authoritative copy of
      the zone (each renders ``type master`` in its named.conf). A
      record change therefore needs to land on *every* server, not
      just the one with ``is_primary=True``. Enqueue one
      ``pending`` op row per server; each agent picks up its own row
      on its next long-poll and applies via loopback nsupdate.
      Pre-#170 the queue only went to the primary, which silently
      broke any multi-server (or supervised-appliance) group — the
      secondaries' on-disk zone files stayed frozen at the bundle
      they received on initial register.

    Returns the op for the primary server (or ``None`` if no primary
    + agent-based path didn't run either). Callers that need to ack
    every server's apply outcome should query ``DNSRecordOp`` directly
    by ``server_id``; the singular return preserves the prior
    contract for the typed-event audit path.
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
        # User flipped the primary off — for agentless, drop the op with a
        # warning rather than hang on a dead WinRM / nsupdate socket at a paused
        # server. (Agent-based groups fall through to the fan-out below, which
        # queues to whatever agent-based servers ARE enabled.)
        if not primary.is_enabled:
            logger.warning(
                "record_op_dropped_server_disabled",
                zone=zone.name,
                server=str(primary.id),
                driver=primary.driver,
                op=op,
                name=record.get("name"),
                type=record.get("type"),
            )
            return None
        return await _apply_agentless(db, primary, zone, op, record, target_serial)

    # Agent-based group: fan out to every ENABLED, agent-based server in the
    # group, independent of whether the designated primary is currently
    # disabled. Gating the whole group on the primary's is_enabled (as we used
    # to) dropped the op for healthy secondaries too — and because the agent's
    # structural_etag excludes records in a no-views group, re-enabling the
    # primary later does NOT flush the missed op, so the edit could strand on
    # every agent (#481). The query mirrors ``resolve_primary_server`` minus the
    # is_primary filter; agentless servers are excluded because their write path
    # is single-server immediate-apply.
    agent_rows = (
        (
            await db.execute(
                select(DNSServer)
                .where(
                    DNSServer.group_id == zone.group_id,
                    DNSServer.is_enabled.is_(True),
                )
                .order_by(DNSServer.is_primary.desc(), DNSServer.created_at)
            )
        )
        .scalars()
        .all()
    )
    agent_servers = [s for s in agent_rows if not is_agentless(s.driver)]
    if not agent_servers:
        # Every agent-based server in the group is disabled (incl. the primary).
        # Nothing can converge right now; log it rather than drop silently.
        logger.warning(
            "record_op_dropped_no_enabled_agent",
            zone=zone.name,
            group_id=str(zone.group_id),
            op=op,
            name=record.get("name"),
            type=record.get("type"),
        )
        return None

    primary_op: DNSRecordOp | None = None
    first_op: DNSRecordOp | None = None
    for srv in agent_servers:
        row = DNSRecordOp(
            server_id=srv.id,
            zone_name=zone.name,
            op=op,
            record=record,
            target_serial=target_serial,
            state="pending",
        )
        db.add(row)
        if first_op is None:
            first_op = row
        if srv.id == primary.id:
            primary_op = row
    await db.flush()
    # #358 — wake every agent in this group so they re-poll + apply the
    # queued op immediately instead of waiting for the belt-and-braces
    # tick. Collected here (no commit yet); the request's
    # ``wake_publishing`` dependency flushes it after the outer commit.
    collect_wake(dns_group_channel(zone.group_id))
    # Return the primary's op when the primary is among the enabled servers,
    # else the first enabled agent's op. The return must be truthy whenever we
    # actually enqueued something — a disabled primary + enabled secondary still
    # dispatches a wire op (#481) — so a caller that gates a DB delete on "was a
    # wire op dispatched?" (e.g. dns bulk-delete) doesn't keep a row whose
    # record was already removed on-wire.
    return primary_op or first_op


async def enqueue_record_ops_batch(
    db: AsyncSession,
    zone: DNSZone,
    ops: list[dict[str, Any]],
) -> list[DNSRecordOp | None]:
    """Batch counterpart to :func:`enqueue_record_op`.

    Groups all ops for a single zone into one driver call when the zone's
    primary is agentless — cuts an N-record sync from N WinRM round trips
    to one. Agent-based primaries fall through to the per-op path since
    they queue in the DB and the agent batches at poll time.

    ``ops`` is a list of ``{op, record, target_serial?}`` dicts matching
    the singular ``enqueue_record_op`` arg shape.

    Returns one ``DNSRecordOp`` (or None on drop) per input op, in the
    same order as ``ops``.
    """
    if not ops:
        return []

    primary = await resolve_primary_server(db, zone)
    if primary is None:
        logger.warning(
            "record_op_batch_dropped_no_primary",
            zone=zone.name,
            group_id=str(zone.group_id),
            count=len(ops),
        )
        return [None] * len(ops)

    if is_agentless(primary.driver):
        # Agentless: drop at a paused server rather than hang on a dead socket.
        if not primary.is_enabled:
            logger.warning(
                "record_op_batch_dropped_server_disabled",
                zone=zone.name,
                server=str(primary.id),
                driver=primary.driver,
                count=len(ops),
            )
            return [None] * len(ops)
        return await _apply_agentless_batch(db, primary, zone, ops)

    # Agent-based: DB rows only; agent will batch at poll time. Delegates to
    # enqueue_record_op per op, which fans out to every ENABLED agent-based
    # server regardless of whether the designated primary is disabled (#481).
    return [
        await enqueue_record_op(db, zone, o["op"], o["record"], o.get("target_serial")) for o in ops
    ]


async def enqueue_record_ops_bulk(
    db: AsyncSession,
    zone: DNSZone,
    ops: list[dict[str, Any]],
) -> int:
    """Enqueue many ops for a SINGLE zone with one server-set resolution.

    The seeding / bulk-import fast-path. :func:`enqueue_record_ops_batch`
    re-resolves the agent server list once *per op* (it loops the singular
    path), which is fine for a handful of records but quadratic-feeling at
    seed scale. This resolves the enabled agent-based servers once and inserts
    all ``DNSRecordOp`` rows in a single ``add_all`` + flush; agentless
    primaries delegate to the existing batched driver call.

    Returns the number of ops dispatched (0 if no enabled primary).
    """
    if not ops:
        return 0

    primary = await resolve_primary_server(db, zone)
    if primary is None:
        logger.warning(
            "record_op_bulk_dropped",
            zone=zone.name,
            group_id=str(zone.group_id),
            count=len(ops),
            reason="no primary configured for zone",
        )
        return 0

    if is_agentless(primary.driver):
        # Agentless: drop at a paused server rather than hang on a dead socket.
        if not primary.is_enabled:
            logger.warning(
                "record_op_bulk_dropped",
                zone=zone.name,
                group_id=str(zone.group_id),
                count=len(ops),
                reason="agentless primary is disabled",
            )
            return 0
        rows = await _apply_agentless_batch(db, primary, zone, ops)
        return sum(1 for r in rows if r is not None)
    # Agent-based falls through: the fan-out below queues to every ENABLED
    # agent-based server regardless of whether the primary is disabled (#481).

    # Agent-based: every enabled agent-based server in the group renders an
    # independent authoritative copy, so each needs its own op rows. Resolve
    # the set ONCE (mirrors enqueue_record_op's fan-out query).
    agent_rows = (
        (
            await db.execute(
                select(DNSServer).where(
                    DNSServer.group_id == zone.group_id,
                    DNSServer.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    agent_servers = [s for s in agent_rows if not is_agentless(s.driver)]
    if not agent_servers:
        return 0
    for srv in agent_servers:
        db.add_all(
            [
                DNSRecordOp(
                    server_id=srv.id,
                    zone_name=zone.name,
                    op=o["op"],
                    record=o["record"],
                    target_serial=o.get("target_serial"),
                    state="pending",
                )
                for o in ops
            ]
        )
    await db.flush()
    # #358 — wake every agent in the group so they drain the queued ops on the
    # next poll instead of the belt-and-braces tick. Flushed after the outer
    # commit by the request's ``wake_publishing`` dependency.
    collect_wake(dns_group_channel(zone.group_id))
    return len(ops)


async def _apply_agentless_batch(
    db: AsyncSession,
    server: DNSServer,
    zone: DNSZone,
    ops: list[dict[str, Any]],
) -> list[DNSRecordOp | None]:
    """Apply many record ops against an agentless server in one driver call.

    Writes per-op DNSRecordOp rows (applied / failed) so the audit trail
    matches what the singular path produces. A whole-batch exception
    (WinRM auth failure, PS parse error in the generated script) marks
    every row failed with the same error — per-op failures (a bad
    record type for example) only mark their own row.
    """
    from app.drivers.dns.base import RecordChange, RecordData  # noqa: PLC0415

    op_rows: list[DNSRecordOp] = []
    for o in ops:
        row = DNSRecordOp(
            server_id=server.id,
            zone_name=zone.name,
            op=o["op"],
            record=o["record"],
            target_serial=o.get("target_serial"),
            state="pending",
        )
        db.add(row)
        op_rows.append(row)
    await db.flush()

    changes = [
        RecordChange(
            op=o["op"],  # type: ignore[arg-type]
            zone_name=zone.name,
            record=RecordData(
                name=o["record"]["name"],
                record_type=o["record"]["type"],
                value=o["record"]["value"],
                ttl=o["record"].get("ttl"),
                priority=o["record"].get("priority"),
                weight=o["record"].get("weight"),
                port=o["record"].get("port"),
            ),
            target_serial=o.get("target_serial") or 0,
        )
        for o in ops
    ]

    driver = get_driver(server.driver)
    try:
        results = await driver.apply_record_changes(server, changes)
    except Exception as exc:  # noqa: BLE001 — whole-batch wire/auth failure
        logger.warning(
            "record_op_batch_failed",
            server=str(server.id),
            driver=server.driver,
            zone=zone.name,
            count=len(op_rows),
            error=str(exc),
        )
        err = str(exc)[:500]
        for row in op_rows:
            row.state = "failed"
            row.attempts = 1
            row.last_error = err
        await db.flush()
        return list(op_rows)

    applied_count = 0
    for row, result in zip(op_rows, results, strict=True):
        row.attempts = 1
        if result.ok:
            row.state = "applied"
            row.applied_at = datetime.now(UTC)
            row.last_error = None
            applied_count += 1
        else:
            row.state = "failed"
            row.last_error = (result.error or "unknown")[:500]
    await db.flush()

    logger.info(
        "record_op_batch_applied_agentless",
        server=str(server.id),
        driver=server.driver,
        zone=zone.name,
        total=len(results),
        applied=applied_count,
        failed=len(results) - applied_count,
    )
    return list(op_rows)


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
