"""The agent config bundle ships pending record ops a PAGE at a time.

Found by the appliance sizing campaign (2026-09-02/03): seeding 250k A+PTR
records into an agent-based group queued 500k ``DNSRecordOp`` rows for the
one agent server, and the next long-poll materialised every one of them into
the bundle — the api reached 4.2 GB, was memcg-killed at its 4Gi limit, and
the same thing happened on every poll, so the bundle never reached the data
plane and the queue could only be cleared by hand.

The bundle now carries at most ``settings.dns_agent_ops_batch`` ops (oldest
first) plus ``pending_ops_remaining``; the shipped page is ``in_flight`` so
the next long-poll — which returns immediately while ops are pending — ships
the next page. Paging must not touch the structural fingerprint, or every
page would look like a config change and force a daemon reload.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.dns import DNSRecordOp, DNSServer, DNSServerGroup, DNSZone
from app.services.dns.agent_config import build_config_bundle


async def _agent_server(db: AsyncSession) -> tuple[DNSServer, DNSZone]:
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
    )
    db.add(zone)
    await db.flush()
    return server, zone


async def _queue_ops(db: AsyncSession, server: DNSServer, zone: DNSZone, n: int) -> None:
    base = datetime.now(UTC) - timedelta(minutes=n)
    for i in range(n):
        db.add(
            DNSRecordOp(
                server_id=server.id,
                zone_name=zone.name,
                op="create",
                record={"name": f"h{i:04d}", "type": "A", "value": f"10.0.{i // 250}.{i % 250}"},
                state="pending",
                created_at=base + timedelta(seconds=i),
            )
        )
    await db.flush()


async def _states(db: AsyncSession, server: DNSServer) -> dict[str, int]:
    rows = (
        (await db.execute(select(DNSRecordOp.state).where(DNSRecordOp.server_id == server.id)))
        .scalars()
        .all()
    )
    out: dict[str, int] = {}
    for s in rows:
        out[s] = out.get(s, 0) + 1
    return out


@pytest.mark.asyncio
async def test_a_backlog_is_shipped_a_page_at_a_time_oldest_first(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dns_agent_ops_batch", 5)
    server, zone = await _agent_server(db_session)
    await _queue_ops(db_session, server, zone, 12)
    await db_session.commit()

    first = await build_config_bundle(db_session, server)
    await db_session.commit()
    page = first["pending_record_ops"]
    assert len(page) == 5
    assert [op["record"]["name"] for op in page] == [f"h{i:04d}" for i in range(5)]
    assert first["pending_ops_remaining"] == 7
    assert await _states(db_session, server) == {"in_flight": 5, "pending": 7}

    second = await build_config_bundle(db_session, server)
    await db_session.commit()
    assert [op["record"]["name"] for op in second["pending_record_ops"]] == [
        f"h{i:04d}" for i in range(5, 10)
    ]
    assert second["pending_ops_remaining"] == 2

    third = await build_config_bundle(db_session, server)
    await db_session.commit()
    assert len(third["pending_record_ops"]) == 2
    assert third["pending_ops_remaining"] == 0
    assert await _states(db_session, server) == {"in_flight": 12}

    # Paging is invisible to the structural fingerprint: no page looks like
    # a config change (which would force a full daemon reload per page).
    assert first["structural_etag"] == second["structural_etag"] == third["structural_etag"]
    # …but the bundle etag differs per page, so the agent never confuses
    # one page's ack set with the next.
    assert len({first["etag"], second["etag"], third["etag"]}) == 3


@pytest.mark.asyncio
async def test_a_backlog_under_the_batch_ships_whole_with_nothing_remaining(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dns_agent_ops_batch", 5000)
    server, zone = await _agent_server(db_session)
    await _queue_ops(db_session, server, zone, 3)
    await db_session.commit()

    bundle = await build_config_bundle(db_session, server)
    assert len(bundle["pending_record_ops"]) == 3
    assert bundle["pending_ops_remaining"] == 0


@pytest.mark.asyncio
async def test_an_empty_queue_reports_no_backlog(db_session: AsyncSession) -> None:
    server, _zone = await _agent_server(db_session)
    await db_session.commit()
    bundle = await build_config_bundle(db_session, server)
    assert bundle["pending_record_ops"] == []
    assert bundle["pending_ops_remaining"] == 0
