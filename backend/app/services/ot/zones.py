"""Purdue zoning lookups for OT devices — issue #542.

An :class:`~app.models.ot.OTZone` is a property of a *subnet*, not of an
address: segmentation in an OT plant is drawn at the VLAN / subnet
boundary, and that is exactly the level an auditor asks about. So the
zone "of" a device is always reached through the subnet its IPAM address
lives in — there is no per-address zone row and there should not be one.

The pairing of a device-declared Purdue level with its zone's level is
what makes the interesting question answerable: a Level-1 PLC sitting in
a Level-4 enterprise subnet is a finding, and today nobody can produce
that report without a spreadsheet.

Read-only, like the rest of ``services/ot`` — one database SELECT and one
pure comparison. No network I/O.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAddress
from app.models.ot import OTDevice, OTZone


async def effective_zone_for_address(db: AsyncSession, ip_address_id: uuid.UUID) -> OTZone | None:
    """Return the OT zone governing ``ip_address_id``, or ``None``.

    Resolution is a single hop — address → its subnet → that subnet's
    zone row — done as one join so the by-address descriptor endpoint
    and the conformity check don't each pay two round-trips.

    ``None`` means one of three indistinguishable-and-equivalent things:
    the address doesn't exist, its subnet has no zone declared, or the
    subnet was never zoned in the first place. All three are "we don't
    know this device's zone", and callers must treat an unknown zone as
    *not a violation* — see :func:`purdue_mismatch`.
    """
    stmt = (
        select(OTZone)
        .join(IPAddress, IPAddress.subnet_id == OTZone.subnet_id)
        .where(IPAddress.id == ip_address_id)
    )
    return (await db.execute(stmt)).scalars().first()


def purdue_mismatch(device: OTDevice, zone: OTZone | None) -> bool | None:
    """Does this device's declared Purdue level disagree with its zone's?

    Returns ``True`` when both levels are known and differ, ``False``
    when both are known and agree, and ``None`` when either side is
    unset.

    The tri-state matters. ``OTDevice.purdue_level`` is nullable on
    purpose (an operator usually knows the protocol long before the
    level) and a device may sit in a subnet nobody has zoned yet. Folding
    those cases into ``True`` would flood the conformity report with
    findings that are really just missing data; folding them into
    ``False`` would silently pass a fleet nobody has classified. An
    unknown is an unknown — the caller decides whether to surface it as
    "unclassified" or ignore it.

    Comparison is on the numeric value, so a device stamped ``3.5`` in a
    zone stored as ``3.50`` agrees (``Decimal`` compares by value, not by
    exponent). That is what lets level 3.5 — the manufacturing/enterprise
    DMZ, and the boundary auditors care most about — be a first-class
    level rather than a spelling convention.
    """
    if zone is None:
        return None
    if device.purdue_level is None or zone.purdue_level is None:
        return None
    return device.purdue_level != zone.purdue_level


def purdue_to_str(value: Decimal | None) -> str | None:
    """Render a stored ``Numeric(2,1)`` level as its canonical string.

    ``Decimal("2.0")`` → ``"2"``, ``Decimal("3.5")`` → ``"3.5"``. The
    column keeps one decimal place so 3.5 — the manufacturing/enterprise
    DMZ — is representable, which means every whole level comes back
    with a trailing ``.0`` that does not match the
    :data:`~app.models.ot.PURDUE_LEVELS` vocabulary the API accepts, nor
    the frontend's ``PURDUE_LEVELS`` label map keyed on the same strings.

    Lives here rather than in the router because three surfaces render
    this value — REST, the CSV importer, and the Copilot tools — and a
    level rendered ``"2.0"`` on one of them silently misses every label
    lookup while looking plausible in the payload.

    Deliberately does *not* validate against the vocabulary: if a row
    somehow holds a level the API wouldn't accept, showing it beats
    silently reporting ``null``.
    """
    if value is None:
        return None
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


__all__ = ["effective_zone_for_address", "purdue_mismatch", "purdue_to_str"]
