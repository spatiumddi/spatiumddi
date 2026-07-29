"""Fragile-device probe suppression — issue #722.

Medical- and OT-device vendors instruct sites *not* to scan their VLANs.
Swisslog's pneumatic-tube deployment guide says to omit ping sweeps and
nmap scans of any range supporting the PTS; the same hazard covers
patient monitors, BMC / IPMI management endpoints, and the PLC / RTU
class ``network.ot`` already registers. Fragile IP stacks fault or fall
off the network under a sweep, and SpatiumDDI ships three features that
actively violate that instruction — the #23 discovery sweep,
``tools.nmap``, and the ``tools.network`` reachability tools.

This module is the one place that answers *"may we send packets at
this?"*. Every prober asks it; nothing re-implements the walk.

Resolution semantics
--------------------

The flag lives on ``IPSpace`` / ``IPBlock`` / ``Subnet`` and **ORs down
the chain**: a scope is suppressed if it, or *any* of its ancestors, sets
``do_not_probe``. This is deliberately unlike
``services.dns.ddns.resolve_effective_ddns``, which walks to the first
non-inheriting ancestor and lets a descendant override its parent. A
hospital that marks its clinical IPSpace do-not-probe must not have one
subnet quietly opt back in, so there is no per-level inherit toggle and
no way to un-set the flag from below. The *first* level found saying so
(nearest-first: subnet, then the block chain upward, then the space)
supplies the reason, because that is the most specific explanation an
operator wrote down.

Fail-safe direction
-------------------

Where this module is unsure, it is unsure in the direction of *fewer*
refusals, not more: a target it cannot map to a subnet is allowed. That
is the honest behaviour for a guard whose input is an arbitrary operator
-supplied host string — refusing everything unmappable would make the
tools page unusable against off-IPAM infrastructure, and the operator
who did not model a network in IPAM never told us it was fragile. It
does mean the flag protects what IPAM knows about, which is the same
scope every other IPAM-derived guard has. :func:`resolve_probe_policy`,
which takes an already-resolved ``Subnet`` row, has no such ambiguity.

Passive collection is out of scope by design: PCAP (#59), DHCP
fingerprinting, RA sniffing and ARP-table reads observe traffic that
already exists. The hazard is packets *we* originate.
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass

from sqlalchemy import func as sa_func
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPAddress, IPBlock, IPSpace, Subnet

# Depth cap for the block walk. ``IPBlock.parent_block_id`` is a
# self-referential FK with no database-level cycle guard, so a row that
# somehow became its own ancestor (a bad restore, a hand-written UPDATE)
# would spin this loop forever inside a request. Real hierarchies are a
# handful of levels deep; 64 is far past any legitimate design and cheap
# to bound.
_MAX_BLOCK_DEPTH = 64


@dataclass(frozen=True)
class ProbeVerdict:
    """Whether a probe may proceed, and — when not — why.

    ``reason`` is the operator's own note from whichever level set the
    flag. It is empty when they set the flag without writing one, which
    is why every caller renders it conditionally rather than assuming a
    sentence is there. ``source`` identifies the level (``"subnet:<id>"``
    / ``"block:<id>"`` / ``"space:<id>"``) so an operator chasing an
    unexpected refusal can find the row that produced it without
    guessing at the hierarchy.
    """

    blocked: bool
    reason: str = ""
    source: str = ""
    scope_label: str = ""

    @property
    def allowed(self) -> bool:
        return not self.blocked

    def message(self, *, action: str = "Active probing") -> str:
        """Operator-facing sentence for a refusal.

        Always names the scope and the level that set the flag; appends
        the operator's reason when there is one. Callers use this for
        the 422 detail and for the skip note in a run summary, so the
        same wording shows up wherever the refusal surfaces.
        """
        where = self.scope_label or self.source or "this scope"
        base = f"{action} is suppressed for {where}: it is marked do-not-probe"
        if self.reason:
            return f"{base} — {self.reason}"
        return base

    def as_dict(self) -> dict[str, object]:
        """JSON-safe form for audit rows and API payloads."""
        return {
            "blocked": self.blocked,
            "reason": self.reason,
            "source": self.source,
            "scope": self.scope_label,
        }


ALLOWED = ProbeVerdict(blocked=False)


async def resolve_probe_policy(db: AsyncSession, subnet: Subnet) -> ProbeVerdict:
    """Effective do-not-probe verdict for one subnet.

    Checks the subnet, then walks its block chain upward, then the
    containing space, stopping at the first level that sets the flag.
    Nearest-first ordering is what makes the reported reason the most
    specific one an operator wrote.

    Soft-deleted ancestors are honoured rather than skipped: a block in
    the trash still describes the gear that is physically there, and
    resuming probes because someone deleted a container row would be the
    wrong way for this to fail.
    """
    if subnet.do_not_probe:
        return ProbeVerdict(
            blocked=True,
            reason=subnet.do_not_probe_reason or "",
            source=f"subnet:{subnet.id}",
            scope_label=str(subnet.network),
        )

    seen: set[uuid.UUID] = set()
    current = await db.get(IPBlock, subnet.block_id) if subnet.block_id else None
    depth = 0
    while current is not None and depth < _MAX_BLOCK_DEPTH:
        if current.id in seen:
            break
        seen.add(current.id)
        depth += 1
        if current.do_not_probe:
            return ProbeVerdict(
                blocked=True,
                reason=current.do_not_probe_reason or "",
                source=f"block:{current.id}",
                scope_label=str(current.network),
            )
        current = (
            await db.get(IPBlock, current.parent_block_id) if current.parent_block_id else None
        )

    space = await db.get(IPSpace, subnet.space_id) if subnet.space_id else None
    if space is not None and space.do_not_probe:
        return ProbeVerdict(
            blocked=True,
            reason=space.do_not_probe_reason or "",
            source=f"space:{space.id}",
            scope_label=space.name,
        )
    return ALLOWED


async def resolve_probe_policy_for_subnet_id(
    db: AsyncSession, subnet_id: uuid.UUID
) -> ProbeVerdict:
    """:func:`resolve_probe_policy` for a subnet id, allowing on a miss.

    A subnet that vanished between the caller's read and this call is
    not a fragile subnet — it is no subnet — so it is allowed, and
    whatever the caller does next will fail on its own missing row.
    """
    subnet = await db.get(Subnet, subnet_id)
    if subnet is None:
        return ALLOWED
    return await resolve_probe_policy(db, subnet)


async def _subnets_covering_ip(db: AsyncSession, addr: str) -> list[Subnet]:
    """Every subnet whose network contains ``addr``.

    Plural because IPSpaces are isolated routing domains and may overlap:
    the same 10.0.0.0/8 address can legitimately exist in three spaces.
    We have no way to tell which one an operator meant from a bare IP, so
    the caller treats a flag on *any* of them as suppressing — the safe
    reading, and the one that matches what the flag is asserting ("this
    address hosts fragile gear").
    """
    stmt = select(Subnet).where(
        Subnet.deleted_at.is_(None),
        Subnet.network.op(">>=")(sa_func.inet(addr)),
    )
    return list((await db.execute(stmt)).scalars().all())


async def _subnets_overlapping_network(db: AsyncSession, cidr: str) -> list[Subnet]:
    """Every subnet that contains, or is contained by, ``cidr``.

    Both directions matter for a CIDR target: scanning ``10.1.0.0/16``
    reaches the hosts of a flagged ``10.1.7.0/24`` inside it, and
    scanning ``10.1.7.128/25`` reaches hosts of a flagged ``10.1.0.0/16``
    around it. Either way packets land on suppressed addresses.
    """
    stmt = select(Subnet).where(
        Subnet.deleted_at.is_(None),
        or_(
            Subnet.network.op(">>=")(sa_func.cidr(cidr)),
            Subnet.network.op("<<=")(sa_func.cidr(cidr)),
        ),
    )
    return list((await db.execute(stmt)).scalars().all())


async def _subnets_for_hostname(db: AsyncSession, name: str) -> list[Subnet]:
    """Subnets of every IPAM address recorded under ``name``.

    A hostname target is matched against ``IPAddress.hostname`` rather
    than resolved through DNS. Two reasons, both about the guard being
    trustworthy: a DNS lookup inside an authorization check makes the
    verdict depend on a network round-trip that can time out or return a
    different answer than the probe will use (a TOCTOU the operator
    cannot see), and it lets a hostile record steer the guard. Matching
    our own inventory is exact, offline, and reflects the same rows the
    operator flagged.

    Matched case-insensitively on the short label, and also against
    ``hostname`` as the first label of a supplied FQDN, because
    operators type both forms into the tools page.
    """
    needle = name.strip().rstrip(".").lower()
    if not needle:
        return []
    candidates = {needle, needle.split(".", 1)[0]}
    stmt = (
        select(Subnet)
        .join(IPAddress, IPAddress.subnet_id == Subnet.id)
        .where(
            Subnet.deleted_at.is_(None),
            sa_func.lower(IPAddress.hostname).in_(candidates),
        )
        .distinct()
    )
    return list((await db.execute(stmt)).scalars().all())


async def check_probe_target(db: AsyncSession, target: str) -> ProbeVerdict:
    """Verdict for an operator-supplied probe target.

    Accepts what the probing surfaces actually accept: a bare IP, a CIDR
    (nmap scans ranges), or a hostname. Resolution per form is documented
    on the three ``_subnets_*`` helpers above.

    Returns the first blocking verdict found across the matched subnets.
    An unmappable target is allowed — see the module docstring on the
    fail-safe direction.
    """
    raw = (target or "").strip()
    if not raw:
        return ALLOWED

    subnets: list[Subnet]
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        try:
            ipaddress.ip_network(raw, strict=False)
        except ValueError:
            subnets = await _subnets_for_hostname(db, raw)
        else:
            subnets = await _subnets_overlapping_network(db, raw)
    else:
        subnets = await _subnets_covering_ip(db, raw)

    for subnet in subnets:
        verdict = await resolve_probe_policy(db, subnet)
        if verdict.blocked:
            return verdict
    return ALLOWED


__all__ = [
    "ALLOWED",
    "ProbeVerdict",
    "check_probe_target",
    "resolve_probe_policy",
    "resolve_probe_policy_for_subnet_id",
]
