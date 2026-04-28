"""SNMP poller Celery tasks.

Three tasks live here:

* ``poll_device`` — single-device pass: locks the row with
  ``SELECT … FOR UPDATE SKIP LOCKED`` so concurrent dispatches don't
  double-poll, runs ``test_connection`` to refresh sys-group metadata,
  then conditional walks of interfaces / arp / fdb. ARP results
  feed into ``cross_reference_arp`` so IPAM ``last_seen_at`` /
  ``mac_address`` columns stay current.
* ``dispatch_due_devices`` — beat-fired every 60 s. Selects devices
  whose ``next_poll_at`` has elapsed and queues per-device tasks.
* ``purge_stale_arp_entries`` — daily housekeeping; deletes
  ``network_arp_entry`` rows whose ``last_seen`` is older than
  the configured retention window.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.celery_app import celery_app
from app.config import settings
from app.models.network import (
    NetworkArpEntry,
    NetworkDevice,
    NetworkFdbEntry,
    NetworkInterface,
)
from app.services.snmp import (
    cross_reference_arp,
    test_connection,
    walk_arp,
    walk_fdb,
    walk_interfaces,
)
from app.services.snmp.errors import (
    SNMPAuthError,
    SNMPProtocolError,
    SNMPTimeoutError,
    SNMPTransportError,
)
from app.services.snmp.poller import ArpData, FdbData, InterfaceData

logger = structlog.get_logger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────


def _classify_error(exc: Exception) -> str:
    """Map a poller exception to ``last_poll_status``."""
    if isinstance(exc, SNMPTimeoutError):
        return "timeout"
    if isinstance(exc, (SNMPAuthError, SNMPTransportError, SNMPProtocolError)):
        return "failed"
    return "failed"


async def _upsert_interfaces(
    db: AsyncSession, device_id: uuid.UUID, rows: Iterable[InterfaceData]
) -> dict[int, uuid.UUID]:
    """Insert / update interfaces by (device_id, if_index).

    Returns a mapping from ``if_index`` → ``interface.id`` for the
    caller's downstream FDB / ARP joins.
    """
    rows_list = list(rows)
    out: dict[int, uuid.UUID] = {}
    if not rows_list:
        return out

    for r in rows_list:
        existing = (
            await db.execute(
                select(NetworkInterface).where(
                    NetworkInterface.device_id == device_id,
                    NetworkInterface.if_index == r.if_index,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            iface = NetworkInterface(
                device_id=device_id,
                if_index=r.if_index,
                name=r.name,
                alias=r.alias,
                description=r.description,
                speed_bps=r.speed_bps,
                mac_address=r.mac_address,
                admin_status=r.admin_status,
                oper_status=r.oper_status,
                last_change_seconds=r.last_change_seconds,
            )
            db.add(iface)
            await db.flush()
            out[r.if_index] = iface.id
        else:
            existing.name = r.name
            existing.alias = r.alias
            existing.description = r.description
            existing.speed_bps = r.speed_bps
            existing.mac_address = r.mac_address
            existing.admin_status = r.admin_status
            existing.oper_status = r.oper_status
            existing.last_change_seconds = r.last_change_seconds
            out[r.if_index] = existing.id

    # Don't delete interfaces missing from this poll — they may simply
    # not have come up, and operators want history. Stale interface
    # rows are cheap; cleanup is operator-initiated.
    return out


async def _upsert_arp(
    db: AsyncSession,
    device: NetworkDevice,
    rows: Iterable[ArpData],
    if_index_to_id: dict[int, uuid.UUID],
) -> int:
    """Upsert ARP rows, marking absent ones ``stale``. Returns count
    inserted / updated this pass (excluding the stale-mark sweep)."""
    rows_list = list(rows)
    seen: set[tuple[str, str | None]] = set()
    now = datetime.now(UTC)

    for r in rows_list:
        seen.add((r.ip_address, None))
        existing = (
            await db.execute(
                select(NetworkArpEntry).where(
                    NetworkArpEntry.device_id == device.id,
                    NetworkArpEntry.ip_address == r.ip_address,
                    NetworkArpEntry.vrf_name.is_(None),
                )
            )
        ).scalar_one_or_none()
        iface_id = if_index_to_id.get(r.if_index) if r.if_index is not None else None
        if existing is None:
            db.add(
                NetworkArpEntry(
                    device_id=device.id,
                    interface_id=iface_id,
                    ip_address=r.ip_address,
                    mac_address=r.mac_address,
                    vrf_name=None,
                    address_type=r.address_type,
                    state=r.state,
                    first_seen=now,
                    last_seen=now,
                )
            )
        else:
            existing.interface_id = iface_id
            existing.mac_address = r.mac_address
            existing.address_type = r.address_type
            existing.state = r.state
            existing.last_seen = now

    # Mark every other ARP row for this device as 'stale' (don't
    # delete — janitor purges later).
    if rows_list:
        present_ips = [r.ip_address for r in rows_list]
        absent_rows = list(
            (
                await db.execute(
                    select(NetworkArpEntry).where(
                        NetworkArpEntry.device_id == device.id,
                        NetworkArpEntry.ip_address.notin_(present_ips),
                        NetworkArpEntry.state != "stale",
                    )
                )
            )
            .scalars()
            .all()
        )
        for absent in absent_rows:
            absent.state = "stale"

    return len(rows_list)


async def _upsert_fdb(
    db: AsyncSession,
    device: NetworkDevice,
    rows: Iterable[FdbData],
    if_index_to_id: dict[int, uuid.UUID],
) -> int:
    """Replace FDB for the device — absence-delete on every poll.

    Bridge FDB entries are short-lived (default 5-min aging). Keeping
    stale rows here would mislead the "where is this MAC?" lookup.
    """
    rows_list = list(rows)

    await db.execute(delete(NetworkFdbEntry).where(NetworkFdbEntry.device_id == device.id))

    for r in rows_list:
        iface_id = if_index_to_id.get(r.if_index)
        if iface_id is None:
            # No matching interface row — skip rather than insert a
            # dangling FK reference.
            continue
        db.add(
            NetworkFdbEntry(
                device_id=device.id,
                interface_id=iface_id,
                mac_address=r.mac_address,
                vlan_id=r.vlan_id,
                fdb_type=r.fdb_type,
            )
        )
    return len(rows_list)


# ── Single-device poll ──────────────────────────────────────────────


async def _poll_device_async(device_id: str) -> dict[str, Any]:
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    summary: dict[str, Any] = {
        "status": "skipped",
        "device_id": device_id,
    }
    try:
        async with factory() as db:
            # SKIP LOCKED so a concurrent beat tick + manual "Poll Now"
            # don't both run; whichever wins takes the lock and the
            # other returns "skipped".
            row = (
                await db.execute(
                    select(NetworkDevice)
                    .where(NetworkDevice.id == uuid.UUID(device_id))
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                summary["status"] = "locked_or_missing"
                return summary

            now = datetime.now(UTC)
            arp_count = 0
            fdb_count = 0
            interface_count = 0
            errors: list[str] = []

            # ── 1. sys-group probe (always run) ───────────────────────
            try:
                sys_info = await test_connection(row)
                row.sys_descr = sys_info.sys_descr
                row.sys_object_id = sys_info.sys_object_id
                row.sys_name = sys_info.sys_name
                row.sys_uptime_seconds = sys_info.sys_uptime_seconds
                if sys_info.vendor and not row.vendor:
                    row.vendor = sys_info.vendor
            except Exception as exc:  # noqa: BLE001 — we classify below
                row.last_poll_at = now
                row.last_poll_status = _classify_error(exc)
                row.last_poll_error = str(exc)[:500]
                row.next_poll_at = now + timedelta(seconds=row.poll_interval_seconds)
                await db.commit()
                summary.update(
                    {
                        "status": row.last_poll_status,
                        "error": str(exc),
                    }
                )
                return summary

            # ── 2. ifTable ───────────────────────────────────────────
            if_index_to_id: dict[int, uuid.UUID] = {}
            if row.poll_interfaces:
                try:
                    ifaces = await walk_interfaces(row)
                    if_index_to_id = await _upsert_interfaces(db, row.id, ifaces)
                    interface_count = len(ifaces)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"interfaces: {exc}")
            else:
                # We still need the if_index → id map for ARP / FDB
                # joins; pull whatever we already have on file.
                existing = list(
                    (
                        await db.execute(
                            select(NetworkInterface).where(NetworkInterface.device_id == row.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if_index_to_id = {i.if_index: i.id for i in existing}

            # ── 3. ARP ───────────────────────────────────────────────
            if row.poll_arp:
                try:
                    arp_rows = await walk_arp(row)
                    arp_count = await _upsert_arp(db, row, arp_rows, if_index_to_id)
                    xref = await cross_reference_arp(db, row, arp_rows)
                    summary["xref"] = xref
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"arp: {exc}")

            # ── 4. FDB ───────────────────────────────────────────────
            if row.poll_fdb:
                try:
                    fdb_rows = await walk_fdb(row)
                    fdb_count = await _upsert_fdb(db, row, fdb_rows, if_index_to_id)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"fdb: {exc}")

            # ── 5. Status roll-up ────────────────────────────────────
            requested = sum(int(x) for x in (row.poll_arp, row.poll_fdb, row.poll_interfaces))
            failed = len(errors)
            if failed == 0:
                row.last_poll_status = "success"
                row.last_poll_error = None
            elif failed < requested:
                row.last_poll_status = "partial"
                row.last_poll_error = "; ".join(errors)[:500]
            else:
                row.last_poll_status = "failed"
                row.last_poll_error = "; ".join(errors)[:500]

            row.last_poll_at = now
            row.last_poll_arp_count = arp_count
            row.last_poll_fdb_count = fdb_count
            row.last_poll_interface_count = interface_count
            row.next_poll_at = now + timedelta(seconds=row.poll_interval_seconds)

            await db.commit()
            summary.update(
                {
                    "status": row.last_poll_status,
                    "arp_count": arp_count,
                    "fdb_count": fdb_count,
                    "interface_count": interface_count,
                    "errors": errors,
                }
            )
            return summary
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.snmp_poll.poll_device", bind=True)
def poll_device(self: Any, device_id_str: str) -> dict[str, Any]:  # noqa: ARG001
    """Poll one device. Idempotent — safe to retry."""
    return asyncio.run(_poll_device_async(device_id_str))


# ── Beat-fired dispatcher ───────────────────────────────────────────


async def _dispatch_due_async() -> int:
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    queued = 0
    try:
        async with factory() as db:
            now = datetime.now(UTC)
            rows = list(
                (
                    await db.execute(
                        select(NetworkDevice).where(
                            NetworkDevice.is_active.is_(True),
                            (NetworkDevice.next_poll_at.is_(None))
                            | (NetworkDevice.next_poll_at <= now),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for r in rows:
                try:
                    poll_device.delay(str(r.id))
                    queued += 1
                except Exception as exc:  # noqa: BLE001 — broker down? give up quietly
                    logger.warning("snmp_dispatch_enqueue_failed", error=str(exc))
                    break
        return queued
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.snmp_poll.dispatch_due_devices", bind=True)
def dispatch_due_devices(self: Any) -> int:  # noqa: ARG001
    """Beat-fired sweep — queues a ``poll_device`` task per due row."""
    return asyncio.run(_dispatch_due_async())


# ── Janitor ─────────────────────────────────────────────────────────


async def _purge_stale_arp_async(days: int) -> int:
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            cutoff = datetime.now(UTC) - timedelta(days=days)
            result = await db.execute(
                delete(NetworkArpEntry).where(NetworkArpEntry.last_seen < cutoff)
            )
            await db.commit()
            return int(result.rowcount or 0)
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.snmp_poll.purge_stale_arp_entries", bind=True)
def purge_stale_arp_entries(self: Any, days: int = 30) -> int:  # noqa: ARG001
    """Delete ARP rows older than ``days`` days. Default 30."""
    return asyncio.run(_purge_stale_arp_async(days))


__all__ = [
    "poll_device",
    "dispatch_due_devices",
    "purge_stale_arp_entries",
]
