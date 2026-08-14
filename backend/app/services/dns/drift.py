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
from app.services.dns.tsig import resolve_group_transfer_key, transfer_needs_tsig

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
    # Conditions that make the diff below less trustworthy than it looks.
    # Rendered verbatim by the UI — keep them operator-readable.
    warnings: list[str] = field(default_factory=list)


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

    # #734 — an agent-managed BIND9 / Technitium grants transfer to the
    # group's TSIG key, not to our address, so the read has to be signed.
    # Resolve once for the whole group rather than per server: every server
    # in a group renders from the same bundle and so grants the same keys.
    # Must happen before the gather() below, which deliberately touches no
    # DB. Skipped entirely when no server needs it, so a group of Windows or
    # operator-run servers never decrypts a secret it has no use for.
    transfer_key = (
        await resolve_group_transfer_key(db, group_id)
        if any(transfer_needs_tsig(s) for s in servers)
        else None
    )

    # Split-horizon caveat. Under views (#24) each view gets its own zone row
    # and its own rendered zone file, but an AXFR is addressed by zone *name*
    # — the server answers with whichever view matches the control plane's
    # source IP, which is not necessarily the view this row belongs to. The
    # diff would then report the other view's content as drift in both
    # directions. We can't tell from the wire which view answered, so say so
    # rather than let an operator "fix" a difference that isn't one.
    if zone.view_id is not None:
        report.warnings.append(
            "This zone belongs to a DNS view. A zone transfer is answered by "
            "whichever view matches the control plane's source address, so "
            "differences below may reflect a different view rather than real drift."
        )
    elif any(r.view_id is not None for r in db_rows):
        report.warnings.append(
            "Some records in this zone are scoped to a specific DNS view and are "
            "only served to clients matching it. They may appear as missing here "
            "even when the server is correct."
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
        # Fail closed, and say which thing is missing (#734). Without a key
        # the transfer is REFUSED, and the generic error sends the operator
        # to allow-transfer / the firewall — neither of which is the problem,
        # and neither of which they can reach anyway, because the agent owns
        # named.conf. Naming the missing key is the difference between a
        # fixable report and a dead end.
        if transfer_needs_tsig(srv):
            if transfer_key is None:
                entry.status = "unsupported"
                entry.error = (
                    "This server's group has no TSIG key, so the zone transfer that "
                    "drift reads cannot be authenticated. Create a TSIG key on the "
                    "group (Servers → group → TSIG keys) and let the agent apply the "
                    "new config, then re-run this report."
                )
                return entry
            srv_tsig = transfer_key
        else:
            # Only sign where an agent actually granted the key. Windows Path
            # A and an operator's own BIND9 both AXFR unsigned and are
            # authorised by address; handing either a key it never granted
            # turns a working pull into NOTAUTH.
            srv_tsig = None
        try:
            on_wire: list[RecordData] = await driver.pull_zone_records(
                srv, zone.name, tsig=srv_tsig
            )
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
