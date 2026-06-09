"""DNS config-drift report (#61)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.dns.base import RecordData
from app.models.dns import DNSRecord, DNSServer, DNSServerGroup, DNSZone
from app.services.dns import drift as drift_mod


class _FakeDriver:
    def __init__(self, records: list[RecordData]) -> None:
        self._records = records

    async def pull_zone_records(self, server: Any, zone_name: str) -> list[RecordData]:
        return list(self._records)


async def _group_server_zone(
    db: AsyncSession, *, server_name: str, zone_name: str
) -> tuple[DNSServerGroup, DNSServer, DNSZone]:
    group = DNSServerGroup(name=f"g-{uuid.uuid4().hex[:6]}")
    db.add(group)
    await db.flush()
    server = DNSServer(
        group_id=group.id,
        name=server_name,
        driver="bind9",
        host="10.0.0.53",
        port=53,
        is_enabled=True,
    )
    db.add(server)
    zone = DNSZone(group_id=group.id, name=zone_name, zone_type="primary", kind="forward")
    db.add(zone)
    await db.flush()
    return group, server, zone


async def test_zone_drift_categorises(db_session: AsyncSession, monkeypatch: Any) -> None:
    group, _server, zone = await _group_server_zone(
        db_session, server_name="ns1", zone_name="drift.example.com."
    )
    db_session.add_all(
        [
            DNSRecord(zone_id=zone.id, name="www", record_type="A", value="10.0.0.1", ttl=300),
            DNSRecord(zone_id=zone.id, name="mail", record_type="A", value="10.0.0.2", ttl=300),
        ]
    )
    await db_session.commit()

    # Live server: www in sync, a rogue record added directly on the host,
    # mail not being served.
    live = [
        RecordData(name="www", record_type="A", value="10.0.0.1", ttl=300),
        RecordData(name="rogue", record_type="A", value="10.0.0.9", ttl=300),
    ]
    monkeypatch.setattr(drift_mod, "get_driver", lambda _d: _FakeDriver(live))

    report = await drift_mod.compute_zone_drift(db_session, group_id=group.id, zone=zone)
    assert report.db_record_count == 2
    assert len(report.servers) == 1
    s = report.servers[0]
    assert s.status == "ok"
    assert s.in_sync == 1  # www matches
    assert {(r.name, r.value) for r in s.extra_on_server} == {("rogue", "10.0.0.9")}
    assert {(r.name, r.value) for r in s.missing_on_server} == {("mail", "10.0.0.2")}
    assert s.drift_count == 2


async def test_zone_drift_pull_failure_is_surfaced(
    db_session: AsyncSession, monkeypatch: Any
) -> None:
    group, _server, zone = await _group_server_zone(
        db_session, server_name="ns-down", zone_name="down.example.com."
    )
    await db_session.commit()

    class _BoomDriver:
        async def pull_zone_records(self, server: Any, zone_name: str) -> list[RecordData]:
            raise RuntimeError("AXFR refused")

    monkeypatch.setattr(drift_mod, "get_driver", lambda _d: _BoomDriver())

    report = await drift_mod.compute_zone_drift(db_session, group_id=group.id, zone=zone)
    assert len(report.servers) == 1
    s = report.servers[0]
    assert s.status == "error"
    assert "AXFR refused" in (s.error or "")
    assert s.drift_count == 0
