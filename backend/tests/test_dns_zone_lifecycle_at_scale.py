"""Zone lifecycle at scale — two findings of the appliance sizing campaign
(2026-09-02, 250k-1M record zones on agent-based groups).

1. ``POST .../records/bulk-delete`` on an agent-based zone deleted NOTHING
   (``deleted: 0``, every record "wire delete failed: unknown") while the
   singular ``DELETE .../records/{id}`` deleted the same rows. Agent-based
   servers never answer inline: the batch enqueue queues their ops as
   ``pending`` for the agent's next long-poll, and the route's gate treated
   anything but ``applied`` as a failure. Only a ``failed`` wire delete may
   keep the DB row.

2. ``DELETE .../zones/{id}?permanent=true`` cascaded through the ORM — every
   record row loaded and deleted one by one in the request — so a large zone
   took minutes of one transaction on the request loop and the api pod was
   killed by its own probes mid-delete. The records now go in one statement
   (the FK already cascades), and the zone's queued ops go with them.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.dns import DNSRecord, DNSRecordOp, DNSServer, DNSServerGroup, DNSZone

# ── helpers ──────────────────────────────────────────────────────────────────


async def _admin_headers(db: AsyncSession) -> dict[str, str]:
    user = User(
        username=f"zl-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Zone Lifecycle Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(user)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _agent_group_and_zone(db: AsyncSession) -> tuple[DNSServerGroup, DNSServer, DNSZone]:
    """One group, one enabled agent-based (bind9) primary, one zone."""
    grp = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(grp)
    await db.flush()
    server = DNSServer(
        group_id=grp.id,
        driver="bind9",
        host="10.0.0.1",
        name=f"srv-{uuid.uuid4().hex[:6]}",
        is_primary=True,
        is_enabled=True,
    )
    db.add(server)
    await db.flush()
    zone = DNSZone(
        group_id=grp.id,
        name=f"z{uuid.uuid4().hex[:6]}.example.",
        zone_type="primary",
        kind="forward",
        primary_ns="ns1.example.",
        admin_email="admin.example.",
        ttl=3600,
    )
    db.add(zone)
    await db.flush()
    return grp, server, zone


async def _add_records(db: AsyncSession, zone: DNSZone, count: int) -> list[DNSRecord]:
    rows = [
        DNSRecord(
            zone_id=zone.id,
            name=f"h{i:06d}",
            fqdn=f"h{i:06d}.{zone.name}",
            record_type="A",
            value=f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}",
        )
        for i in range(count)
    ]
    db.add_all(rows)
    await db.flush()
    return rows


def _zone_url(zone: DNSZone) -> str:
    return f"/api/v1/dns/groups/{zone.group_id}/zones/{zone.id}"


async def _record_count(db: AsyncSession, zone_id: uuid.UUID) -> int:
    return int(
        (await db.execute(select(func.count()).where(DNSRecord.zone_id == zone_id))).scalar_one()
    )


async def _ops(db: AsyncSession, zone_name: str) -> list[DNSRecordOp]:
    return list(
        (await db.execute(select(DNSRecordOp).where(DNSRecordOp.zone_name == zone_name)))
        .scalars()
        .all()
    )


# ── 1. bulk delete on an agent-based zone ────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_delete_on_an_agent_zone_deletes_the_rows_and_queues_the_ops(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _admin_headers(db_session)
    _grp, _server, zone = await _agent_group_and_zone(db_session)
    rows = await _add_records(db_session, zone, 5)
    await db_session.commit()

    resp = await client.post(
        f"{_zone_url(zone)}/records/bulk-delete",
        headers=headers,
        json={"record_ids": [str(r.id) for r in rows[:3]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The wire ops are PENDING on an agent-based server (the agent applies
    # them on its next long-poll); that is a dispatched delete, not a failed
    # one, and the DB rows must go.
    assert body["deleted"] == 3, body
    assert body["skipped"] == [], body
    assert await _record_count(db_session, zone.id) == 2
    ops = [o for o in await _ops(db_session, zone.name) if o.op == "delete"]
    assert len(ops) == 3
    assert {o.state for o in ops} == {"pending"}


@pytest.mark.asyncio
async def test_bulk_delete_keeps_the_row_only_when_the_wire_delete_failed(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1.dns import router as dns_router

    headers = await _admin_headers(db_session)
    _grp, _server, zone = await _agent_group_and_zone(db_session)
    rows = await _add_records(db_session, zone, 3)
    await db_session.commit()

    # One failed wire delete, one applied, one still pending.
    states = ["failed", "applied", "pending"]

    async def fake_batch(db, zone_, ops):  # noqa: ANN001
        return [
            SimpleNamespace(state=s, last_error="primary refused" if s == "failed" else None)
            for s in states
        ]

    monkeypatch.setattr(dns_router, "enqueue_record_ops_batch", fake_batch)
    resp = await client.post(
        f"{_zone_url(zone)}/records/bulk-delete",
        headers=headers,
        json={"record_ids": [str(r.id) for r in rows]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 2, body
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["record_id"] == str(rows[0].id)
    assert "primary refused" in body["skipped"][0]["reason"]
    # The failed one is still published, so it is still in the database.
    assert await _record_count(db_session, zone.id) == 1
