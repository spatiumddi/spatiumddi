"""BACnet device-registry invariants — issue #541 Phase 1.

Three helpers live here rather than inline in the router because each
is about the *registry*, not about a request shape, and each must
behave identically whichever surface (REST, importer, Copilot
proposal) drives it:

* :func:`sync_subnet_id` keeps ``BACnetDevice.subnet_id`` in step with
  the IPAM address the device is anchored to. That column is a
  denormalised copy — it exists so the "exactly one BBMD per subnet"
  check can group by subnet without joining ``ip_address`` on every
  evaluation — and a denormalised copy is only trustworthy if it is
  refreshed at every point the anchor can move.
* :func:`assert_instance_available` turns the ``uq_bacnet_device_instance``
  unique constraint into a domain error that names the *conflicting*
  device. "Instance 100 is already used by AHU-3" is the answer an
  integrator needs; a raw ``IntegrityError`` surfacing as a 500 is not.
  The database stays the real guard — this is the friendly path, not
  the enforcement.
* :func:`next_available_instance` backs the UI's suggest button, so an
  operator commissioning a panel gets a free number instead of
  guessing and re-submitting into a 409.

Zero network I/O: nothing in this package speaks BACnet. Populating
these rows from the wire needs an on-segment agent with a raw UDP
broadcast socket (``Who-Is`` on 47808) plus an agent→subnet binding
that isn't modelled today — deferred, see ``app.models.bacnet``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bacnet import BACNET_INSTANCE_MAX, BACNET_INSTANCE_MIN, BACnetDevice
from app.models.ipam import IPAddress


class DeviceInstanceConflict(Exception):
    """A device instance number is already registered to another row.

    Carries the conflicting device's identity because "who has it" is
    the only fact that actually helps resolve a collision — the caller
    turns this into a 409 whose message names the other device rather
    than restating that the number is taken.
    """

    def __init__(
        self,
        *,
        device_instance: int,
        existing_id: uuid.UUID,
        existing_name: str,
    ) -> None:
        self.device_instance = device_instance
        self.existing_id = existing_id
        self.existing_name = existing_name
        # Unnamed devices are common right after an import, so fall
        # back to the id — an operator can still paste it into a
        # lookup, whereas "used by ''" is a dead end.
        label = existing_name.strip() if existing_name else ""
        super().__init__(
            f"BACnet device instance {device_instance} is already used by "
            f"{label or existing_id}"
        )


async def sync_subnet_id(db: AsyncSession, device: BACnetDevice) -> uuid.UUID | None:
    """Point ``device.subnet_id`` at its IPAM address's current subnet.

    Call on create, and on any update that changes ``ip_address_id``.
    Mutates the passed instance in place (no flush, no commit — the
    caller owns the transaction) and returns the resolved subnet id.

    ``None`` means the anchoring address row is gone, which the FK's
    ``ON DELETE SET NULL`` shape already anticipates: a device whose
    subnet is unknown drops out of the per-subnet BBMD grouping rather
    than being silently counted against the wrong subnet.
    """
    subnet_id = (
        await db.execute(select(IPAddress.subnet_id).where(IPAddress.id == device.ip_address_id))
    ).scalar_one_or_none()
    device.subnet_id = subnet_id
    return subnet_id


async def assert_instance_available(
    db: AsyncSession,
    device_instance: int,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Raise :class:`DeviceInstanceConflict` when the instance is taken.

    ``exclude_id`` is the row being edited, so re-submitting a device's
    own instance number in a PATCH isn't reported as a conflict with
    itself.

    This is a check-then-act against a uniquely-indexed column, so it
    is racy by construction under concurrent creates; that is fine and
    intentional. The DB constraint is what guarantees uniqueness, and
    the caller re-maps the resulting ``IntegrityError`` to the same
    409. This function exists to make the *common* case legible.
    """
    stmt = select(BACnetDevice.id, BACnetDevice.device_name).where(
        BACnetDevice.device_instance == device_instance
    )
    if exclude_id is not None:
        stmt = stmt.where(BACnetDevice.id != exclude_id)
    row = (await db.execute(stmt.limit(1))).first()
    if row is None:
        return
    raise DeviceInstanceConflict(
        device_instance=device_instance,
        existing_id=row[0],
        existing_name=row[1] or "",
    )


async def next_available_instance(db: AsyncSession, start: int = BACNET_INSTANCE_MIN) -> int | None:
    """Lowest unused device instance at or above ``start``, or ``None``.

    Walks the taken instances in ascending order and returns the first
    gap, so a registry with holes (devices decommissioned out of the
    middle of a range) re-uses them instead of drifting upward forever.
    Sites conventionally block-allocate instances per building or per
    trade, which is what ``start`` is for: pass the base of the block
    and get the next free number *inside* it.

    The scan reads one uniquely-indexed integer column and a BACnet
    internetwork tops out in the thousands of devices, so a gap-walk in
    Python is both cheaper to reason about and easier to be sure of
    than a window-function query.

    ``None`` means everything from ``start`` to ``BACNET_INSTANCE_MAX``
    is taken. In practice that reports a ``start`` near the 22-bit
    ceiling, not four million real devices.
    """
    if start < BACNET_INSTANCE_MIN:
        start = BACNET_INSTANCE_MIN
    if start > BACNET_INSTANCE_MAX:
        return None

    taken = (
        (
            await db.execute(
                select(BACnetDevice.device_instance)
                .where(BACnetDevice.device_instance >= start)
                .order_by(BACnetDevice.device_instance.asc())
            )
        )
        .scalars()
        .all()
    )

    candidate = start
    for value in taken:
        if value > candidate:
            # First gap in the ascending run — everything below is
            # contiguous from ``start``, so ``candidate`` is free.
            break
        candidate = value + 1

    if candidate > BACNET_INSTANCE_MAX:
        return None
    return candidate


__all__ = [
    "DeviceInstanceConflict",
    "assert_instance_available",
    "next_available_instance",
    "sync_subnet_id",
]
