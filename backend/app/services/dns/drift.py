"""Per-server DNS config-drift report (#61).

Extends the zone-serial drift surface with a full record-level diff: for
every server in the zone's group, AXFR / pull the live zone and diff it
against the SpatiumDDI DB source of truth, surfacing per server what's
**extra on the server** (records present on the wire but not in the DB —
a manual change made directly on the host) and what's **missing on the
server** (DB rows the server isn't serving). Read-only — never applies.

Reuses ``pull_from_server._key`` for the identity/normalisation so the
comparison matches the additive-sync path exactly (relative-vs-FQDN and
TTL-only differences don't register as drift). A record whose *value*
changed on a server surfaces as a missing+extra pair, since the key
includes the value.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.dns import get_driver
from app.drivers.dns.base import RecordData
from app.models.dns import DNSRecord, DNSServer, DNSZone
from app.services.dns.pull_from_server import _key

logger = structlog.get_logger(__name__)


@dataclass
class DriftRecord:
    name: str
    record_type: str
    value: str
    ttl: int | None = None


@dataclass
class ServerDrift:
    server_id: str
    server_name: str
    driver: str
    status: str  # "ok" | "error" | "unsupported"
    error: str | None = None
    in_sync: int = 0
    extra_on_server: list[DriftRecord] = field(default_factory=list)
    missing_on_server: list[DriftRecord] = field(default_factory=list)

    @property
    def drift_count(self) -> int:
        return len(self.extra_on_server) + len(self.missing_on_server)


@dataclass
class ZoneDriftReport:
    zone_id: str
    zone_name: str
    db_record_count: int
    servers: list[ServerDrift] = field(default_factory=list)


def _to_drift_record(r: RecordData | DNSRecord) -> DriftRecord:
    return DriftRecord(
        name=r.name or "@",
        record_type=r.record_type,
        value=r.value,
        ttl=r.ttl,
    )


async def compute_zone_drift(
    db: AsyncSession, *, group_id: uuid.UUID, zone: DNSZone
) -> ZoneDriftReport:
    """Compute per-server record-level drift for ``zone`` across every
    server in ``group_id``. Each server is pulled independently; a pull
    failure (unreachable / paused / driver can't AXFR) is surfaced as an
    ``error`` entry rather than failing the whole report."""
    db_rows = list(
        (await db.execute(select(DNSRecord).where(DNSRecord.zone_id == zone.id))).scalars().all()
    )
    db_by_key = {_key(r, zone.name): r for r in db_rows}

    servers = list(
        (
            await db.execute(
                select(DNSServer).where(DNSServer.group_id == group_id).order_by(DNSServer.name)
            )
        )
        .scalars()
        .all()
    )

    report = ZoneDriftReport(
        zone_id=str(zone.id), zone_name=zone.name, db_record_count=len(db_rows)
    )

    async def _drift_for_server(srv: DNSServer) -> ServerDrift:
        entry = ServerDrift(
            server_id=str(srv.id),
            server_name=srv.name,
            driver=srv.driver,
            status="ok",
        )
        driver = get_driver(srv.driver)
        if not hasattr(driver, "pull_zone_records"):
            entry.status = "unsupported"
            entry.error = f"Driver {srv.driver!r} can't pull live records for drift."
            return entry
        try:
            on_wire: list[RecordData] = await driver.pull_zone_records(srv, zone.name)
        except Exception as exc:  # noqa: BLE001 — per-server, never fail the whole report
            entry.status = "error"
            entry.error = str(exc)
            logger.warning(
                "dns.drift.pull_failed",
                zone=zone.name,
                server=str(srv.id),
                driver=srv.driver,
                error=str(exc),
            )
            return entry

        wire_by_key = {_key(r, zone.name): r for r in on_wire}
        entry.extra_on_server = [
            _to_drift_record(r) for k, r in wire_by_key.items() if k not in db_by_key
        ]
        entry.missing_on_server = [
            _to_drift_record(r) for k, r in db_by_key.items() if k not in wire_by_key
        ]
        entry.in_sync = len(set(db_by_key) & set(wire_by_key))
        return entry

    # Pull every server concurrently — a slow/unreachable host shouldn't add
    # its full AXFR timeout serially to the request latency. Each coroutine
    # only touches the driver (network), never the shared AsyncSession, and
    # isolates its own failures, so gather() is safe. Order is preserved.
    report.servers = list(await asyncio.gather(*(_drift_for_server(s) for s in servers)))

    return report
