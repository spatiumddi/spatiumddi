"""#481 — a disabled agent-based primary must not drop record ops for the
whole group.

enqueue_record_op / _batch / _bulk gated the entire function on the designated
primary's is_enabled, returning before the per-enabled-agent fan-out. So a
record edit while the primary was paused (e.g. maintenance) queued to NO agent
in the group — not just the disabled primary — and (in a no-views group) never
self-healed. The fan-out now runs for every ENABLED agent-based server
regardless of the primary's state; only agentless primaries still drop-on-paused.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dns import DNSRecordOp, DNSServer, DNSServerGroup, DNSZone
from app.services.dns.record_ops import (
    enqueue_record_op,
    enqueue_record_ops_bulk,
)


async def _group_disabled_primary_enabled_secondary(
    db: AsyncSession,
) -> tuple[DNSServer, DNSServer, DNSZone]:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    primary = DNSServer(
        group_id=grp.id,
        driver="bind9",
        host="10.0.0.1",
        name="primary",
        is_primary=True,
        is_enabled=False,  # paused
    )
    secondary = DNSServer(
        group_id=grp.id,
        driver="bind9",
        host="10.0.0.2",
        name="secondary",
        is_primary=False,
        is_enabled=True,
    )
    db.add_all([primary, secondary])
    await db.flush()
    zone = DNSZone(
        group_id=grp.id,
        name="corp.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.corp.example.",
        admin_email="admin.corp.example.",
    )
    db.add(zone)
    await db.flush()
    return primary, secondary, zone


async def _ops_for_zone(db: AsyncSession, zone: DNSZone) -> list[DNSRecordOp]:
    return list(
        (await db.execute(select(DNSRecordOp).where(DNSRecordOp.zone_name == zone.name)))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_disabled_primary_still_queues_to_enabled_secondary(
    db_session: AsyncSession,
) -> None:
    primary, secondary, zone = await _group_disabled_primary_enabled_secondary(db_session)
    await db_session.commit()

    await enqueue_record_op(
        db_session, zone, "create", {"name": "web", "type": "A", "value": "10.0.0.50"}
    )
    await db_session.commit()

    server_ids = {o.server_id for o in await _ops_for_zone(db_session, zone)}
    assert secondary.id in server_ids  # enabled secondary got the op
    assert primary.id not in server_ids  # disabled primary did not


@pytest.mark.asyncio
async def test_bulk_disabled_primary_still_queues_to_enabled_secondary(
    db_session: AsyncSession,
) -> None:
    primary, secondary, zone = await _group_disabled_primary_enabled_secondary(db_session)
    await db_session.commit()

    dispatched = await enqueue_record_ops_bulk(
        db_session,
        zone,
        [
            {
                "op": "create",
                "record": {"name": "web", "type": "A", "value": "10.0.0.50"},
            }
        ],
    )
    await db_session.commit()

    assert dispatched == 1
    server_ids = {o.server_id for o in await _ops_for_zone(db_session, zone)}
    assert secondary.id in server_ids
    assert primary.id not in server_ids


@pytest.mark.asyncio
async def test_enqueue_returns_truthy_op_when_fanned_out_to_secondary(
    db_session: AsyncSession,
) -> None:
    # The return must be truthy whenever an op was actually enqueued — a caller
    # that gates a DB delete on "was a wire op dispatched?" (dns bulk-delete)
    # would otherwise keep a row whose record was already removed on-wire.
    primary, secondary, zone = await _group_disabled_primary_enabled_secondary(db_session)
    await db_session.commit()

    op = await enqueue_record_op(
        db_session, zone, "delete", {"name": "web", "type": "A", "value": "10.0.0.50"}
    )
    assert op is not None
    assert op.server_id == secondary.id  # the enabled agent's op, not the disabled primary's
