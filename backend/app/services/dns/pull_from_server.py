"""Bi-directional DNS zone sync between SpatiumDDI's DB and the zone's
authoritative server.

Two phases, both additive:

1. **Pull** — AXFR the server and create ``DNSRecord`` rows for anything
   on the wire that's missing from our DB.
2. **Push** — for every record in our DB (after the pull) that isn't on
   the wire, send an RFC 2136 update via the driver's
   ``apply_record_change`` so it lands on the server.

Neither phase deletes anything. Delete intent flows through the normal
record-deletion UI (which already pushes a ``delete`` op through the
driver). Destructive reconciliation (three-way diff + confirmation UI)
is a later iteration.

Driver-agnostic via ``DNSDriver.pull_zone_records`` + the existing
``apply_record_change``. Only the Windows driver implements the pull
side today; BIND9 can follow the same pattern using AXFR from the
agent's loopback named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.drivers.dns import get_driver
from app.drivers.dns.base import RecordChange, RecordData
from app.models.dns import DNSRecord, DNSZone
from app.services.dns.record_ops import resolve_primary_server
from app.services.dns.serial import bump_zone_serial

logger = structlog.get_logger(__name__)


@dataclass
class PullResult:
    """What happened during a pull-from-server operation."""

    server_records: int  # count read from the wire
    existing_in_db: int  # count already in DB that matched
    imported: int  # count of rows we created
    imported_records: list[dict[str, Any]]  # user-visible list for UI
    skipped_unsupported: int  # count filtered because record_type not imported


@dataclass
class PushResult:
    """What happened during a push-to-server operation."""

    candidates: int  # DB rows considered (in DB but not on wire)
    pushed: int  # successfully applied on the wire
    pushed_records: list[dict[str, Any]]  # user-visible list for UI
    push_errors: list[str]  # one entry per failed push (first ~10)


@dataclass
class SyncResult:
    """Combined pull + push result returned by ``sync_zone_with_server``."""

    pull: PullResult
    push: PushResult


# Record types we import — matches the IP/host model. Excludes zone-level
# metadata (SOA/apex NS), security-heavy types that need their own editor
# (DNSKEY, DS, RRSIG), and IDN puns.
_IMPORTABLE_TYPES = {
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "TXT",
    "SRV",
    "PTR",
    "NS",  # non-apex NS only; the driver filters apex NS already
    "TLSA",
}


# Record types whose value is a DNS name. Same-zone targets are sometimes
# stored relative ("aaa") and sometimes absolute ("aaa.zone.example."). A
# naive string compare would treat those as distinct and import a duplicate.
# ``_normalize_value`` folds both forms into a single canonical shape before
# we key on them.
_NAME_VALUED_TYPES = frozenset({"CNAME", "NS", "PTR", "MX", "SRV"})


def _normalize_value(rtype: str, value: str, zone_name: str) -> str:
    """Canonicalise a record value for dedup.

    * Name-valued types: lowercase, ensure trailing dot, and expand bare
      labels against the zone origin. "aaa", "aaa.", "AAA.ZONE.LOCAL." and
      "aaa.zone.local" all collapse to "aaa.zone.local." once the zone is
      "zone.local.".
    * Everything else: stripped + lowercased (fine for A/AAAA/IP literals
      where case doesn't matter either).
    """
    v = (value or "").strip().lower()
    if rtype not in _NAME_VALUED_TYPES:
        return v
    # "@" means the zone apex.
    if v == "@" or v == "":
        return zone_name.lower()
    # Already absolute.
    if v.endswith("."):
        return v
    # Bare label (no dot or not fully qualified) → append zone.
    if "." not in v:
        return f"{v}.{zone_name}".lower()
    # Qualified but missing trailing dot → add it.
    return f"{v}.".lower()


def _key(r: RecordData | DNSRecord, zone_name: str) -> tuple[str, str, str]:
    """Identity key for dedup: (name, type, canonical-value). TTL-only
    differences don't count — neither does relative-vs-FQDN storage for
    name-valued records."""
    name = (r.name or "").strip().lower()
    rtype = r.record_type.upper()
    return (name, rtype, _normalize_value(rtype, r.value, zone_name))


async def _resolve_primary_and_driver(db: AsyncSession, zone: DNSZone) -> tuple[Any, Any]:
    """Shared preamble for both pull and sync: find the zone's primary,
    sanity-check the driver supports pulling records."""
    primary = await resolve_primary_server(db, zone)
    if primary is None:
        raise ValueError(
            "No primary DNS server is configured in this zone's group. "
            "Mark one of the group's servers as primary first."
        )
    driver = get_driver(primary.driver)
    if not hasattr(driver, "pull_zone_records"):
        raise ValueError(
            f"Driver {primary.driver!r} does not support syncing with the "
            "authoritative server. Only Windows DNS (RFC 2136 via AXFR) is "
            "implemented today."
        )
    return primary, driver


def _additive_import(
    db: AsyncSession,
    zone: DNSZone,
    on_wire: list[RecordData],
    db_keys: set[tuple[str, str, str]],
    *,
    apply: bool,
) -> PullResult:
    """Create DNSRecord rows for on-wire entries that are missing from DB."""
    zone_name = zone.name
    zone_name_no_dot = zone.name.rstrip(".")
    imported_records: list[dict[str, Any]] = []
    skipped_unsupported = 0
    existing = 0
    imported = 0

    for rec in on_wire:
        rtype = rec.record_type.upper()
        if rtype not in _IMPORTABLE_TYPES:
            skipped_unsupported += 1
            continue
        if _key(rec, zone_name) in db_keys:
            existing += 1
            continue

        fqdn = zone_name_no_dot if rec.name == "@" else f"{rec.name}.{zone_name_no_dot}"
        row = DNSRecord(
            zone_id=zone.id,
            name=rec.name,
            fqdn=fqdn,
            record_type=rtype,
            value=rec.value,
            ttl=rec.ttl,
            priority=rec.priority,
            weight=rec.weight,
            port=rec.port,
            auto_generated=False,
            ip_address_id=None,
        )
        if apply:
            db.add(row)
        imported += 1
        imported_records.append(
            {
                "name": rec.name,
                "fqdn": fqdn,
                "record_type": rtype,
                "value": rec.value,
                "ttl": rec.ttl,
            }
        )

    return PullResult(
        server_records=len(on_wire),
        existing_in_db=existing,
        imported=imported,
        imported_records=imported_records,
        skipped_unsupported=skipped_unsupported,
    )


async def pull_zone_from_server(
    db: AsyncSession,
    zone: DNSZone,
    *,
    apply: bool = True,
) -> PullResult:
    """One-way pull: read the zone from the primary server, additively
    import anything missing from DB. No push phase. Used by the scheduled
    task when the admin wants read-only sync; for the UI "Sync with server"
    button see ``sync_zone_with_server``.
    """
    primary, driver = await _resolve_primary_and_driver(db, zone)

    on_wire: list[RecordData] = await driver.pull_zone_records(primary, zone.name)  # type: ignore[attr-defined]

    db_rows_res = await db.execute(select(DNSRecord).where(DNSRecord.zone_id == zone.id))
    db_rows = list(db_rows_res.scalars().all())
    db_keys = {_key(r, zone.name) for r in db_rows}

    result = _additive_import(db, zone, on_wire, db_keys, apply=apply)
    if apply and result.imported:
        await db.flush()

    logger.info(
        "dns.pull_from_server",
        zone=zone.name,
        server=str(primary.id),
        driver=primary.driver,
        on_wire=len(on_wire),
        existing=result.existing_in_db,
        imported=result.imported,
        skipped=result.skipped_unsupported,
        mode="apply" if apply else "preview",
    )
    return result


# Record types the push phase will send to the server via apply_record_change.
# Mirrors the Windows driver's _SUPPORTED_RECORD_TYPES but importing that
# would couple us to the driver module — keep a copy here and let drivers
# raise if they really don't support something at call time.
_PUSHABLE_TYPES = frozenset({"A", "AAAA", "CNAME", "MX", "TXT", "PTR", "SRV", "NS", "TLSA"})


async def _additive_push(
    db: AsyncSession,
    primary: Any,
    driver: Any,
    zone: DNSZone,
    on_wire: list[RecordData],
    db_rows: list[DNSRecord],
    *,
    apply: bool,
) -> PushResult:
    """For every DB row whose key isn't on the wire, send a create op via
    the driver. Errors are collected, not raised, so one bad record
    doesn't abort the whole sync.

    Dispatch uses ``apply_record_changes`` (plural) so agentless drivers
    (Windows DNS) can ship the whole batch in one WinRM round trip
    instead of one per record. The ABC default falls back to a
    sequential loop for agent-based drivers (BIND9) where the control
    plane never calls the record writer anyway.
    """
    on_wire_keys = {_key(r, zone.name) for r in on_wire}

    pushed_records: list[dict[str, Any]] = []
    push_errors: list[str] = []

    target_serial = bump_zone_serial(zone) if apply else 0

    # Build the candidate list — DB rows whose (name, type, value) isn't
    # already on the wire. Keeping this as a list (not a generator) so we
    # can zip the driver's per-op results back onto the source rows by
    # index after dispatch.
    candidate_rows: list[DNSRecord] = []
    for row in db_rows:
        rtype = row.record_type.upper()
        if rtype not in _PUSHABLE_TYPES:
            continue
        if _key(row, zone.name) in on_wire_keys:
            continue
        candidate_rows.append(row)

    candidates = len(candidate_rows)

    if not apply:
        for row in candidate_rows:
            pushed_records.append(
                {
                    "name": row.name,
                    "fqdn": row.fqdn,
                    "record_type": row.record_type.upper(),
                    "value": row.value,
                    "ttl": row.ttl,
                }
            )
        return PushResult(
            candidates=candidates,
            pushed=candidates,
            pushed_records=pushed_records,
            push_errors=push_errors,
        )

    changes: list[RecordChange] = [
        RecordChange(
            op="create",
            zone_name=zone.name,
            record=RecordData(
                name=row.name,
                record_type=row.record_type.upper(),
                value=row.value,
                ttl=row.ttl,
                priority=row.priority,
                weight=row.weight,
                port=row.port,
            ),
            target_serial=target_serial,
        )
        for row in candidate_rows
    ]

    if not changes:
        return PushResult(
            candidates=0, pushed=0, pushed_records=pushed_records, push_errors=push_errors
        )

    try:
        results = await driver.apply_record_changes(primary, changes)
    except Exception as exc:  # noqa: BLE001 — whole-batch failure, surface once
        logger.warning(
            "dns.push_drift_batch_failed",
            zone=zone.name,
            server=str(primary.id),
            count=len(changes),
            error=str(exc),
        )
        return PushResult(
            candidates=candidates,
            pushed=0,
            pushed_records=pushed_records,
            push_errors=[f"batch failed: {exc}"],
        )

    pushed = 0
    for row, result in zip(candidate_rows, results, strict=True):
        rtype = row.record_type.upper()
        if result.ok:
            pushed += 1
            pushed_records.append(
                {
                    "name": row.name,
                    "fqdn": row.fqdn,
                    "record_type": rtype,
                    "value": row.value,
                    "ttl": row.ttl,
                }
            )
            continue
        err = f"{row.name} {rtype}: {result.error}"
        logger.warning(
            "dns.push_drift_failed",
            zone=zone.name,
            server=str(primary.id),
            name=row.name,
            record_type=rtype,
            error=result.error,
        )
        if len(push_errors) < 10:
            push_errors.append(err)

    return PushResult(
        candidates=candidates,
        pushed=pushed,
        pushed_records=pushed_records,
        push_errors=push_errors,
    )


async def sync_zone_with_server(
    db: AsyncSession,
    zone: DNSZone,
    *,
    apply: bool = True,
) -> SyncResult:
    """Bi-directional additive sync between DB and the zone's primary server.

    1. AXFR the server once.
    2. Pull phase: import on-wire records missing from DB.
    3. Push phase: for every DB row not on the wire, send an RFC 2136 add.

    Never deletes. Returns counts for both phases so the UI can surface
    them in one pass.
    """
    primary, driver = await _resolve_primary_and_driver(db, zone)

    on_wire: list[RecordData] = await driver.pull_zone_records(primary, zone.name)  # type: ignore[attr-defined]

    # Snapshot DB state BEFORE the pull so we can compute the push set
    # against the "old" DB. We still import new rows from the pull into
    # the DB first; those obviously live on the wire so they never need
    # pushing — they'd be no-ops on the server anyway, but filtering them
    # out keeps the push count honest.
    db_rows_res = await db.execute(select(DNSRecord).where(DNSRecord.zone_id == zone.id))
    db_rows = list(db_rows_res.scalars().all())
    db_keys = {_key(r, zone.name) for r in db_rows}

    pull_result = _additive_import(db, zone, on_wire, db_keys, apply=apply)
    if apply and pull_result.imported:
        await db.flush()

    push_result = await _additive_push(db, primary, driver, zone, on_wire, db_rows, apply=apply)

    logger.info(
        "dns.sync_with_server",
        zone=zone.name,
        server=str(primary.id),
        driver=primary.driver,
        on_wire=len(on_wire),
        imported=pull_result.imported,
        pushed=push_result.pushed,
        push_errors=len(push_result.push_errors),
        mode="apply" if apply else "preview",
    )
    return SyncResult(pull=pull_result, push=push_result)
