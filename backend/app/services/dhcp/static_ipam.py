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

KNOWN GAP — ``services.dhcp.pull_leases._upsert_scope`` still Core-DELETEs a
Windows scope's reservations and re-inserts them from the wire without going
through here, so a UI-created reservation's mirror is stranded on the next
Windows scope sync. It is deliberately NOT fixed with a plain detach: that
reconciler runs on a schedule, and a detach would tear down and recreate the
forward A record on every pass for reservations that never changed. It needs a
re-point-by-IP reconcile instead. Tracked separately.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dhcp import DHCPScope, DHCPStaticAssignment
from app.models.ipam import IPAddress, Subnet
from app.services.dhcp.ipam_mirror import insert_ipam_mirror_row

__all__ = [
    "detach_ipam_for_scope_statics",
    "detach_ipam_for_static",
    "upsert_ipam_for_static",
]


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


async def detach_ipam_for_scope_statics(
    db: AsyncSession,
    scope_id: uuid.UUID,
    *,
    to_status: str = "available",
) -> int:
    """Detach the IPAM mirror of every reservation under ``scope_id``.

    Called by the paths that are about to destroy the reservations physically
    (scope permanent-delete, trash permanent-delete, purge sweep) — the FK
    CASCADE that removes them runs no Python, so the mirror has to be released
    here, *before* the delete, while the rows are still readable.

    ``include_deleted`` because by this point the reservations are themselves
    soft-deleted (stamped as part of their scope's batch), so the global filter
    would otherwise hide the very rows we need to clean up.

    Returns the number of reservations processed.
    """
    res = await db.execute(
        select(DHCPStaticAssignment)
        .where(DHCPStaticAssignment.scope_id == scope_id)
        .execution_options(include_deleted=True)
    )
    statics = list(res.scalars().all())
    for st in statics:
        await detach_ipam_for_static(db, st, to_status=to_status)
    return len(statics)
