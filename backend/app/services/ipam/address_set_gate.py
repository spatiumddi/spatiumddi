"""Address-set write-delegation gate (issue #103).

A caller without subnet-wide ``write`` may still mutate an IP if it falls
inside an :class:`~app.models.address_set.AddressSet` they hold
``write``/``admin`` on. The control plane resolves the caller's writable
ranges on a subnet ONCE per request (:func:`load_writable_set_ranges`) and
then checks each candidate IP against them (:func:`user_can_write_ip`).

Both the interactive IPAM router and the import path consume these helpers;
they live here (not inside ``ipam.router``) so neither side reaches across
modules via a function-local import that would break only at runtime if a
helper were renamed (#12).

Representation (#10): contiguous sets become ``(start_int, end_int)`` interval
tuples (O(interval-count) membership), and explicit sets become a hash SET of
packed host ints (O(1) membership) so a large explicit set doesn't degrade to
an O(n) linear scan per candidate IP.
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import user_has_permission
from app.models.address_set import AddressSet

logger = structlog.get_logger(__name__)


def _pack_ip(value: object) -> int | None:
    """Pack an IP (string / INET) into its integer form, or ``None`` if it
    can't be parsed. Shared by every IP→int conversion in this module so the
    packing rule lives in exactly one place (#12)."""
    try:
        return int(ipaddress.ip_address(str(value)))
    except (ValueError, TypeError):
        return None


@dataclass
class WritableSetRanges:
    """The caller's address-set-delegated writable space on one subnet.

    * ``intervals`` — contiguous ``(start_int, end_int)`` spans (inclusive).
    * ``hosts`` — packed ints for explicit-set members; O(1) membership.

    ``bool(ranges)`` is True iff the caller can write at least one address via
    delegation, so call sites can keep the prior ``if not set_ranges`` idiom.
    """

    intervals: list[tuple[int, int]] = field(default_factory=list)
    hosts: set[int] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.intervals) or bool(self.hosts)

    def contains(self, ip_int: int) -> bool:
        if ip_int in self.hosts:
            return True
        return any(s <= ip_int <= e for s, e in self.intervals)


async def load_writable_set_ranges(
    db: AsyncSession, user: Any, subnet_id: uuid.UUID
) -> WritableSetRanges:
    """Return the :class:`WritableSetRanges` the ``user`` can WRITE on this subnet.

    A set counts when the user holds ``write`` on its address_set id — ``admin``
    implies ``write`` via the permission matcher, so a SINGLE ``write`` check per
    set is sufficient (#9; the old code resolved ``write`` then ``admin``,
    doubling the role-tree walk). Empty when none — the caller still must hold
    subnet write for any IP outside these ranges.
    """
    rows = await db.execute(
        select(
            AddressSet.id,
            AddressSet.range_kind,
            AddressSet.start_address,
            AddressSet.end_address,
            AddressSet.explicit_addresses,
        ).where(AddressSet.subnet_id == subnet_id)
    )
    ranges = WritableSetRanges()
    for set_id, range_kind, start_addr, end_addr, explicit in rows.all():
        # ``admin`` implies ``write`` through ``_action_matches`` — one check.
        if not user_has_permission(user, "write", "address_set", set_id):
            continue
        if range_kind == "contiguous" and start_addr is not None and end_addr is not None:
            s = _pack_ip(start_addr)
            e = _pack_ip(end_addr)
            if s is None or e is None:
                continue
            if s > e:
                # Guarded by a CHECK constraint + API validation, so this
                # should never fire; keep the defensive swap but flag it as a
                # data-integrity signal if it ever does (#13).
                logger.warning(
                    "address_set_inverted_bounds",
                    address_set_id=str(set_id),
                    subnet_id=str(subnet_id),
                    start_address=str(start_addr),
                    end_address=str(end_addr),
                )
                s, e = e, s
            ranges.intervals.append((s, e))
        else:
            for raw in explicit or []:
                v = _pack_ip(raw)
                if v is not None:
                    ranges.hosts.add(v)
    return ranges


def user_can_write_ip(
    user: Any,
    ip_int: int,
    subnet_writable: bool,
    set_ranges: WritableSetRanges,
) -> bool:
    """True if the caller may mutate this IP — either subnet-wide write, or the
    IP falls inside an address set they hold write/admin on (#103)."""
    return subnet_writable or set_ranges.contains(ip_int)
