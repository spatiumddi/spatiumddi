"""IPAM mirror lifecycle for DHCP static reservations (#618).

A reservation owns an ``ip_address`` row: ``status="static_dhcp"``, back-linked
via ``IPAddress.static_assignment_id``, so the subnet view shows the
reservation alongside regular addresses and a dynamic pool can't hand the
address out from under it.

These helpers used to live inside ``api/v1/dhcp/statics.py`` and were therefore
only reachable from the per-reservation CRUD handlers. The paths that destroy
reservations *wholesale* skipped them, because those paths delete through FK
CASCADE (or a Core ``DELETE``) and run no per-row Python. The result was an
``ip_address`` row stranded at ``status="static_dhcp"`` pointing at a
reservation Postgres had already removed: not allocated, not free, not
reclaimable by any sweeper.

They live here so those paths can reuse them without importing an HTTP router.
Wired in as of #618:

* scope permanent-delete (``ai.operations_risky._apply_delete_scope``)
* trash permanent-delete (``api.v1.admin.trash.permanent_delete_from_trash``)
* the nightly purge sweep (``tasks.trash_purge``)
* DHCP server-group delete (``ai.operations_risky._apply_delete_group``)
* DHCP-import ``overwrite`` (``services.dhcp_import.commit``)

``services.dhcp.pull_leases._upsert_scope`` — the Windows scope reconciler —
used to be the one path that destroyed reservations without coming through
here: it Core-DELETEd every reservation under a scope and re-inserted them from
the wire, stranding the mirror of any reservation an operator had created in
the UI. #620 fixed it by making that reconciler diff-merge instead of replace,
so a reservation keeps its id across polls and its mirror's back-link stays
valid. It calls ``upsert_ipam_for_static`` only for reservations that actually
changed (a schedule-driven detach/re-attach would have torn down and recreated
the forward A record on every pass), and ``remove_ipam_for_static`` for the ones
that genuinely vanished from the server.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPScope, DHCPStaticAssignment
from app.models.ipam import IPAddress, Subnet
from app.services.dhcp.ipam_mirror import insert_ipam_mirror_row

__all__ = [
    "detach_ipam_for_static",
    "remirror_scope_statics",
    "remove_ipam_for_scope_statics",
    "remove_ipam_for_static",
    "upsert_ipam_for_static",
]


# Operator-authored columns on the ``ip_address`` mirror that a wholesale
# reservation delete would otherwise lose (the DHCP-derived columns —
# status / hostname / mac / back-links — are re-derived from the static on
# restore, so they're excluded). ``uuid`` / ``datetime`` / ``date`` values are
# JSON-encoded to ISO strings on snapshot and parsed back on restore.
_OPERATOR_MIRROR_FIELDS: tuple[str, ...] = (
    "description",
    "tags",
    "custom_fields",
    "owner_user_id",
    "owner_group_id",
    "managed_by",
    "role",
    "reserved_until",
    "decom_date",
)


def _snapshot_operator_fields(row: IPAddress) -> dict[str, Any] | None:
    """Capture the operator-authored columns of ``row`` as a JSON-safe dict.

    Returns ``None`` when every field is at its empty/default value, so we
    don't persist a snapshot that carries nothing.
    """
    snap: dict[str, Any] = {}
    for field in _OPERATOR_MIRROR_FIELDS:
        val = getattr(row, field, None)
        if val in (None, "", {}, []):
            continue
        if isinstance(val, uuid.UUID):
            snap[field] = str(val)
        elif isinstance(val, (datetime, date)):
            snap[field] = val.isoformat()
        else:
            snap[field] = val
    return snap or None


def _restore_operator_fields(row: IPAddress, snapshot: dict[str, Any]) -> None:
    """Re-apply a :func:`_snapshot_operator_fields` dict onto a fresh mirror row."""
    for field, val in snapshot.items():
        if field not in _OPERATOR_MIRROR_FIELDS:
            continue
        try:
            if field in ("owner_user_id", "owner_group_id") and isinstance(val, str):
                setattr(row, field, uuid.UUID(val))
            elif field == "reserved_until" and isinstance(val, str):
                setattr(row, field, datetime.fromisoformat(val))
            elif field == "decom_date" and isinstance(val, str):
                setattr(row, field, date.fromisoformat(val))
            else:
                setattr(row, field, val)
        except (ValueError, TypeError):
            # A malformed snapshot value must never break restore.
            continue


async def upsert_ipam_for_static(
    db: AsyncSession,
    scope: DHCPScope,
    st: DHCPStaticAssignment,
    *,
    action: str = "create",
) -> None:
    """Create or update the IPAM row mirroring a static DHCP assignment.

    The static is the source of truth for hostname/MAC; IPAM reflects it with
    ``status='static_dhcp'`` and a back-link via ``static_assignment_id`` so the
    subnet view shows the reservation alongside regular addresses.
    """
    ip_str = str(st.ip_address)
    # Detach any previous IPAM row that was pointing at this static (IP change).
    prior = await db.execute(select(IPAddress).where(IPAddress.static_assignment_id == str(st.id)))
    for row in prior.scalars().all():
        if str(row.address) == ip_str:
            continue
        row.static_assignment_id = None
        if row.status == "static_dhcp":
            row.status = "allocated"
    # Find or create the IPAM row for this IP within the scope's subnet.
    res = await db.execute(
        select(IPAddress).where(IPAddress.subnet_id == scope.subnet_id, IPAddress.address == ip_str)
    )
    row = res.scalar_one_or_none()
    if row is None:
        # #564 — a concurrent Kea agent lease-event / Sync-DHCP writer
        # may have already mirrored a dynamic lease at this IP. Insert
        # inside a savepoint so the unique-violation self-heals into the
        # incumbent row (which we then overwrite to static_dhcp — the
        # static is the source of truth) instead of 500-ing on
        # uq_ip_address_subnet_address.
        candidate = IPAddress(subnet_id=scope.subnet_id, address=ip_str, status="static_dhcp")
        row, _created = await insert_ipam_mirror_row(db, candidate)
    row.hostname = st.hostname or row.hostname
    row.mac_address = str(st.mac_address)
    row.status = "static_dhcp"
    row.static_assignment_id = str(st.id)
    # Restore any operator-authored columns captured when this reservation's
    # mirror was deleted (lossless Trash restore, #630), then clear the
    # snapshot so it can't go stale or re-apply on a later ordinary edit.
    if st.ipam_metadata_snapshot:
        _restore_operator_fields(row, st.ipam_metadata_snapshot)
        st.ipam_metadata_snapshot = None
    await db.flush()
    st.ip_address_id = row.id
    # Fire DNS sync so forward/reverse records follow the static.
    from app.api.v1.ipam.router import _sync_dns_record  # noqa: PLC0415

    subnet_row = await db.get(Subnet, scope.subnet_id)
    if subnet_row is not None and row.hostname:
        try:
            await _sync_dns_record(db, row, subnet_row, action=action)
        except Exception:  # noqa: BLE001 — DNS sync is best-effort
            pass


async def detach_ipam_for_static(
    db: AsyncSession,
    st: DHCPStaticAssignment,
    *,
    to_status: str = "available",
) -> None:
    """Release the IPAM row back to ``available`` when the static is removed.

    Also tears down the forward A (DNS sync with action=delete).

    The row is freed to ``available`` (not ``allocated``): the IP no longer
    holds a reservation, and — crucially — a leftover ``allocated`` /
    ``auto_from_lease=False`` row is skipped by the agent's lease-mirror refresh
    (it only re-mirrors ``available`` or ``auto_from_lease`` rows), so it would
    shadow a future dynamic lease at that IP AND never be reaped. ``available``
    lets a new lease reclaim the row (#478).

    ``to_status="reserved"`` is the opt-in "hold the address in IPAM after the
    DHCP config is gone" variant — the caller must be an explicitly destructive
    path that asked for it.
    """
    from app.api.v1.ipam.router import _sync_dns_record  # noqa: PLC0415

    res = await db.execute(select(IPAddress).where(IPAddress.static_assignment_id == str(st.id)))
    for row in res.scalars().all():
        subnet_row = await db.get(Subnet, row.subnet_id)
        if subnet_row is not None:
            try:
                await _sync_dns_record(db, row, subnet_row, action="delete")
            except Exception:  # noqa: BLE001 — DNS sync is best-effort
                pass
        row.static_assignment_id = None
        if row.status == "static_dhcp":
            row.status = to_status


async def remove_ipam_for_static(db: AsyncSession, st: DHCPStaticAssignment) -> int:
    """DELETE the IPAM mirror row(s) for a reservation (not just free them).

    ``detach_ipam_for_static`` sets ``status="available"`` and keeps the row.
    But a persisted ``available`` row still renders as an explicit line in the
    IPAM subnet table (the frontend paints one row per address; "free" is the
    *absence* of a row), so a former reservation kept lingering visibly after
    its scope was deleted — and kept counting toward the subnet's utilization.
    This deletes the ``ip_address`` mirror so the IP folds back into a
    "N free · click to allocate" gap and drops out of the allocated count.

    Tears down the forward/reverse DNS first (same as the detach path). Used by
    the wholesale reservation-removal paths (scope / group / import / purge).
    Returns the number of rows removed.
    """
    from app.api.v1.ipam.router import _sync_dns_record  # noqa: PLC0415

    res = await db.execute(select(IPAddress).where(IPAddress.static_assignment_id == str(st.id)))
    removed = 0
    for row in res.scalars().all():
        # Snapshot operator-authored columns onto the (soft-deleted, retained)
        # reservation before we hard-delete the mirror, so a Trash restore is
        # lossless (#630). The last mirror row wins — there is realistically one.
        snapshot = _snapshot_operator_fields(row)
        if snapshot is not None:
            st.ipam_metadata_snapshot = snapshot
        subnet_row = await db.get(Subnet, row.subnet_id)
        if subnet_row is not None:
            try:
                await _sync_dns_record(db, row, subnet_row, action="delete")
            except Exception:  # noqa: BLE001 — DNS sync is best-effort
                pass
        # Clear the forward FK before the delete so the ORM's in-memory
        # ``st`` doesn't hang onto a stale id (the DB FK is ON DELETE SET NULL).
        if st.ip_address_id == row.id:
            st.ip_address_id = None
        await db.delete(row)
        removed += 1
    return removed


async def remove_ipam_for_scope_statics(db: AsyncSession, scope_id: uuid.UUID) -> int:
    """DELETE the IPAM mirror of every reservation under ``scope_id``.

    The delete-the-row, scope-wide counterpart to ``remove_ipam_for_static``
    (see it for why deleting, not freeing, is required). Uses
    ``include_deleted`` because the reservations may already be soft-deleted as
    part of their scope's batch by the time this runs. Returns the number of
    mirror rows removed.
    """
    res = await db.execute(
        select(DHCPStaticAssignment)
        .where(DHCPStaticAssignment.scope_id == scope_id)
        .execution_options(include_deleted=True)
    )
    removed = 0
    for st in res.scalars().all():
        removed += await remove_ipam_for_static(db, st)
    return removed


async def remirror_scope_statics(db: AsyncSession, scope: DHCPScope) -> int:
    """Re-create the IPAM mirror for each of a restored scope's reservations.

    Counterpart to ``remove_ipam_for_scope_statics``: soft-deleting a scope now
    deletes its ``static_dhcp`` mirror rows, so a Trash restore has to put them
    back. ``upsert_ipam_for_static`` re-creates the row (status + back-link) and
    re-syncs DNS; its #564 savepoint self-heal reclaims the IP if it was taken
    during the Trash window (the static is the source of truth). Call AFTER the
    batch has been un-stamped so the statics are visible. Returns the count.
    """
    res = await db.execute(
        select(DHCPStaticAssignment).where(DHCPStaticAssignment.scope_id == scope.id)
    )
    statics = list(res.scalars().all())
    for st in statics:
        await upsert_ipam_for_static(db, scope, st, action="create")
    return len(statics)
