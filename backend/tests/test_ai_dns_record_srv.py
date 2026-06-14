"""#424 — MCP create_dns_record gained weight + port for SRV records.

The Operator Copilot's ``create_dns_record`` proposal previously accepted
only ``priority``, so it could not create a valid SRV (weight + port stayed
NULL → rendered as 0). It now takes weight + port and rejects an SRV that
omits any of priority / weight / port, mirroring the REST API guard.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.auth import User
from app.models.dns import DNSRecord, DNSRecordOp, DNSServer, DNSServerGroup, DNSZone
from app.services.ai.operations import CreateDNSRecordArgs, get_operation


async def _user(db: AsyncSession) -> User:
    u = User(
        username=f"ai-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="AI Writer",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    u.groups = []  # mark loaded — is_effective_superadmin walks .groups
    db.add(u)
    await db.flush()
    return u


async def _zone(db: AsyncSession) -> DNSZone:
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    # An agent-based server so enqueue_record_op has somewhere to queue to
    # (proves the MCP apply now propagates — issue #424 / pre-existing gap).
    db.add(
        DNSServer(
            group_id=grp.id,
            driver="bind9",
            host="bind9.example.com",
            name=f"srv-{uuid.uuid4().hex[:6]}",
            # enqueue_record_op requires a primary server in the group (it
            # gates "is this group configured?" before fanning out to every
            # agent-based server — same as the REST path).
            is_primary=True,
        )
    )
    zone = DNSZone(
        group_id=grp.id,
        name="example.com.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.example.com.",
        admin_email="admin.example.com.",
    )
    db.add(zone)
    await db.flush()
    return zone


@pytest.mark.asyncio
async def test_srv_preview_apply_sets_weight_and_port(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    zone = await _zone(db_session)
    await db_session.commit()

    op = get_operation("create_dns_record")
    assert op is not None
    args = CreateDNSRecordArgs(
        zone_id=str(zone.id),
        name="_sip._tcp",
        record_type="SRV",
        value="sip.example.com.",
        priority=10,
        weight=20,
        port=5060,
    )
    preview = await op.preview(db_session, user, args)
    assert preview.ok, preview.detail
    assert "weight=20" in preview.preview_text and "port=5060" in preview.preview_text

    result = await op.apply(db_session, user, args)
    row = await db_session.get(DNSRecord, uuid.UUID(result["id"]))
    assert row is not None
    assert (row.priority, row.weight, row.port) == (10, 20, 5060)

    # The apply must propagate: a record op is queued for the agent with
    # the structured fields intact (without this the Copilot's SRV lands
    # in the DB but never reaches BIND9).
    op_row = (
        (await db_session.execute(select(DNSRecordOp).where(DNSRecordOp.op == "create")))
        .scalars()
        .first()
    )
    assert op_row is not None
    assert (
        op_row.record["weight"] == 20
        and op_row.record["port"] == 5060
        and op_row.record["priority"] == 10
    )


@pytest.mark.asyncio
async def test_srv_preview_rejects_missing_port(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    zone = await _zone(db_session)
    await db_session.commit()

    op = get_operation("create_dns_record")
    assert op is not None
    args = CreateDNSRecordArgs(
        zone_id=str(zone.id),
        name="_sip._tcp",
        record_type="SRV",
        value="sip.example.com.",
        priority=10,
        weight=20,
    )
    preview = await op.preview(db_session, user, args)
    assert not preview.ok
    assert "SRV records require" in preview.detail
    with pytest.raises(ValueError, match="SRV records require"):
        await op.apply(db_session, user, args)


@pytest.mark.asyncio
async def test_non_srv_record_ignores_weight_port(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    zone = await _zone(db_session)
    await db_session.commit()

    op = get_operation("create_dns_record")
    assert op is not None
    # An A record never carries weight/port even if the model accepts the
    # fields — apply nulls them for non-SRV types.
    args = CreateDNSRecordArgs(
        zone_id=str(zone.id),
        name="www",
        record_type="A",
        value="10.0.0.1",
    )
    result = await op.apply(db_session, user, args)
    row = await db_session.get(DNSRecord, uuid.UUID(result["id"]))
    assert row is not None
    assert row.weight is None and row.port is None
