"""Reserved-range containment maths for the AV vertical (#540).

Operators declare which multicast ranges belong to which AV protocol
(``AVReservedRange``). Two questions fall out of that declaration, and
this module answers both without any network I/O:

1. **"Which protocol owns this address?"** —
   :func:`resolve_range_for_address`. Longest-prefix wins, because
   nesting is the normal shape: a plant may declare
   ``239.0.0.0/8`` for AES67 and carve ``239.69.0.0/16`` out of it for
   Dante, and a ``239.69.x`` address must resolve to Dante.

2. **"Is this allocation about to land on somebody else's range?"** —
   :func:`check_allocation_conflict`, the widened collision guard the
   issue asks for. Generic IPAM can only tell you an address is free;
   with a declared range we can tell you it is free *and wrong*.

``AVReservedRange.exclusive`` is what separates a hard conflict from a
note. ``exclusive=True`` means "this range is *for* Dante and nothing
else may live here" — a foreign-protocol allocation inside it is a
conflict the operator should resolve. ``exclusive=False`` means "Dante
lives here, among other things" — the same allocation is reported, but
only as information. Small shops run one flat ``239.x`` for everything
and would otherwise face a permanent wall of warnings.

Deliberately NOT here: range-vs-range overlap policy at declaration
time. Nested carve-outs are legitimate (see the Dante-inside-AES67
example above), so refusing an overlapping *declaration* would reject
the vendor defaults themselves. Overlap only becomes interesting when
something is actually allocated, which is what
:func:`check_allocation_conflict` covers.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.av import AV_DEFAULT_RANGES, AVReservedRange

# Either IP family; the two ``ipaddress`` network classes share no
# common base that carries ``subnet_of`` / ``overlaps``, so the union
# is the honest annotation.
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Report verdicts. Strings rather than an enum for the same reason the
# models use frozensets — they cross the REST boundary verbatim and a
# new verdict shouldn't need a schema change on either side.
STATUS_OK = "ok"
STATUS_INFORMATIONAL = "informational"
STATUS_CONFLICT = "conflict"

# How a matched range relates to the proposed allocation.
RELATION_INSIDE = "inside"
RELATION_OVERLAPS = "overlaps"


@dataclass(frozen=True)
class ReservedRangeMatch:
    """One declared range that intersects a proposed allocation.

    Flattened out of the ORM row on purpose: the report crosses into
    the REST layer and the Copilot tools, neither of which should hold
    a live ``AVReservedRange`` past the session that loaded it.
    """

    range_id: uuid.UUID
    cidr: str
    av_protocol: str
    exclusive: bool
    name: str
    # ``inside`` when the allocation sits wholly within the range;
    # ``overlaps`` when it straddles the boundary (only reachable when
    # the caller asks about a CIDR rather than a single address).
    relation: str


@dataclass(frozen=True)
class AllocationConflictReport:
    """Verdict on "may I allocate ``target`` for ``av_protocol`` here?".

    ``status`` is the single field a UI needs to colour the banner;
    everything else exists so the operator can see *why*.
    """

    space_id: uuid.UUID
    # Normalised form of what the caller asked about — a bare address
    # comes back as its host network (``/32`` / ``/128``).
    target: str
    av_protocol: str
    status: str

    # Foreign-protocol ranges marked ``exclusive`` — the hard cases.
    conflicts: list[ReservedRangeMatch] = field(default_factory=list)
    # Foreign-protocol ranges NOT marked exclusive — reported, allowed.
    advisories: list[ReservedRangeMatch] = field(default_factory=list)
    # Ranges belonging to the allocation's own protocol. Presence here
    # is the happy path: the flow is landing where it was planned to.
    own_ranges: list[ReservedRangeMatch] = field(default_factory=list)

    # True when the protocol HAS declared ranges in this space (same
    # address family) but the target sits in none of them. Distinct
    # from a foreign-range conflict: nobody else is claiming the
    # address, the operator has simply strayed off their own plan.
    outside_declared_range: bool = False

    # Well-known default for the protocol, surfaced only when there is
    # something to fix so the UI can offer a one-click "declare the
    # standard range". ``None`` for protocols with no vendor default
    # (SMPTE 2110 plants carve their own).
    suggested_range: str | None = None

    detail: str = ""


def _as_network(value: object) -> IPNetwork | None:
    """Coerce a stored ``CIDR`` column to an ``ipaddress`` network.

    asyncpg hands ``CIDR`` columns back as ``IPv4Network`` /
    ``IPv6Network`` objects, but the same attribute is a plain string
    on a model instance that hasn't round-tripped through the database
    yet (a freshly ``db.add``-ed row inside the same transaction, which
    is exactly the shape a create-then-check handler produces). Accept
    both rather than making every call site guess.
    """
    if isinstance(value, ipaddress.IPv4Network | ipaddress.IPv6Network):
        return value
    if isinstance(value, str):
        try:
            return ipaddress.ip_network(value, strict=False)
        except ValueError:
            # Unreachable through the API — the Postgres ``CIDR`` type
            # rejects a malformed value at the DB boundary and the
            # router validates before insert. Only hand-written SQL can
            # get here, and a preview should skip the bad row rather
            # than 500 the whole request.
            return None
    return None


def parse_target(value: str) -> IPNetwork:
    """Parse an operator-supplied address *or* CIDR into a network.

    A bare literal becomes its host network (``/32`` / ``/128``) so the
    containment maths below has exactly one shape to reason about.
    A prefix with host bits set is widened to its network (``strict=
    False``) — an operator pasting ``239.69.1.5/16`` means the range,
    and the alternative is a 422 that teaches them nothing.

    Raises ``ValueError`` for anything that is neither; callers in the
    REST layer turn that into a 422.
    """
    text = value.strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        # Not a bare literal. Fall through to the network parse, whose
        # own ValueError is the one worth reporting.
        return ipaddress.ip_network(text, strict=False)
    return ipaddress.ip_network(addr)


async def _load_ranges(db: AsyncSession, space_id: uuid.UUID) -> Sequence[AVReservedRange]:
    """Every declared range in one IPSpace.

    Loaded whole and filtered in Python rather than pushed into a
    Postgres ``>>=`` predicate: the row count per space is operator-
    authored (single digits in practice, a few dozen in a large 2110
    plant), and doing the maths in stdlib ``ipaddress`` keeps the
    longest-prefix + relation logic in one readable place instead of
    split across SQL and Python.
    """
    return (
        (await db.execute(select(AVReservedRange).where(AVReservedRange.space_id == space_id)))
        .scalars()
        .all()
    )


def _contains(outer: IPNetwork, inner: IPNetwork) -> bool:
    """``inner`` sits wholly inside ``outer``.

    Split on family rather than calling ``subnet_of`` directly because
    it raises ``TypeError`` on a cross-family comparison — and because
    a ``.version ==`` guard doesn't narrow the union for mypy, so every
    call site would otherwise need a cast.
    """
    if isinstance(outer, ipaddress.IPv4Network) and isinstance(inner, ipaddress.IPv4Network):
        return inner.subnet_of(outer)
    if isinstance(outer, ipaddress.IPv6Network) and isinstance(inner, ipaddress.IPv6Network):
        return inner.subnet_of(outer)
    return False


def _match(row: AVReservedRange, net: IPNetwork, target: IPNetwork) -> ReservedRangeMatch:
    return ReservedRangeMatch(
        range_id=row.id,
        cidr=str(net),
        av_protocol=row.av_protocol,
        exclusive=bool(row.exclusive),
        name=row.name,
        relation=RELATION_INSIDE if _contains(net, target) else RELATION_OVERLAPS,
    )


async def resolve_range_for_address(
    db: AsyncSession, space_id: uuid.UUID, address: str
) -> AVReservedRange | None:
    """The most specific declared range containing ``address``.

    Longest prefix wins so a carve-out beats its parent — a plant that
    declares ``239.0.0.0/8`` for AES67 and ``239.69.0.0/16`` for Dante
    must resolve ``239.69.4.7`` to Dante, not AES67.

    Returns ``None`` when nothing contains the address (including the
    common case of an operator who hasn't declared any ranges yet) or
    when ``address`` doesn't parse — an unparseable address is
    contained by nothing, and raising would make every caller wrap this
    in a try block for no gain.
    """
    try:
        target = parse_target(address)
    except ValueError:
        return None

    best: AVReservedRange | None = None
    best_prefixlen = -1
    for row in await _load_ranges(db, space_id):
        net = _as_network(row.cidr)
        if net is None or net.version != target.version:
            continue
        if _contains(net, target) and net.prefixlen > best_prefixlen:
            best, best_prefixlen = row, net.prefixlen
    return best


async def check_allocation_conflict(
    db: AsyncSession,
    space_id: uuid.UUID,
    address_or_cidr: str,
    av_protocol: str,
) -> AllocationConflictReport:
    """Would allocating ``address_or_cidr`` for ``av_protocol`` step on
    somebody else's declared range?

    The widened collision guard from #540: plain IPAM can only say an
    address is unused, which is the wrong question in an AoIP plant
    where the Dante auto-assign pool is "free" right up until Dante
    tries to use it.

    Verdicts:

    * ``conflict`` — the allocation lands in a foreign protocol's
      **exclusive** range. The operator should move it or drop the
      exclusivity flag.
    * ``informational`` — either a foreign but *shared* range (the
      operator has already said mixing is fine here), or the protocol's
      own declared ranges exist and this allocation is outside all of
      them. Worth showing, never worth blocking.
    * ``ok`` — nothing to say.

    **Longest prefix decides**, matching
    :func:`resolve_range_for_address`. Containing ranges necessarily
    nest, so a broader declaration is superseded by the carve-out
    inside it and does not drive the verdict: allocating Dante into a
    Dante ``239.69.0.0/16`` is clean even when that ``/16`` was carved
    out of an exclusive AES67 ``239.0.0.0/8``, because the operator
    declared the carve-out precisely to say so. Superseded ranges are
    still reported when they belong to the caller's own protocol
    (useful context) and dropped when they don't (noise).

    Ranges the allocation merely *straddles* — reachable only when the
    caller asks about a CIDR — have no containment order to resolve, so
    every one of them counts.

    Read-only: no rows are written and the caller's transaction is
    untouched. Raises ``ValueError`` when ``address_or_cidr`` doesn't
    parse, which the REST layer reports as a 422.
    """
    target = parse_target(address_or_cidr)

    conflicts: list[ReservedRangeMatch] = []
    advisories: list[ReservedRangeMatch] = []
    own_ranges: list[ReservedRangeMatch] = []
    protocol_has_declared_range = False

    containing: list[tuple[AVReservedRange, IPNetwork]] = []
    straddling: list[tuple[AVReservedRange, IPNetwork]] = []
    for row in await _load_ranges(db, space_id):
        net = _as_network(row.cidr)
        if net is None or net.version != target.version:
            continue
        if row.av_protocol == av_protocol:
            # Counted before the overlap test on purpose: this is what
            # makes ``outside_declared_range`` mean "you have a plan and
            # this isn't in it" rather than "you have no plan".
            protocol_has_declared_range = True
        if not net.overlaps(target):
            continue
        (containing if _contains(net, target) else straddling).append((row, net))

    # The winner. Containing ranges form a chain (they all hold the same
    # target, so each is a subnet of the next), which means exactly one
    # sits at the longest prefix — no tie to break.
    winning_prefixlen = max((net.prefixlen for _, net in containing), default=-1)

    for row, net in containing:
        match = _match(row, net, target)
        if row.av_protocol == av_protocol:
            own_ranges.append(match)
        elif net.prefixlen < winning_prefixlen:
            # Superseded by a more specific declaration — see the
            # longest-prefix rule in the docstring.
            continue
        elif row.exclusive:
            conflicts.append(match)
        else:
            advisories.append(match)

    for row, net in straddling:
        match = _match(row, net, target)
        if row.av_protocol == av_protocol:
            own_ranges.append(match)
        elif row.exclusive:
            conflicts.append(match)
        else:
            advisories.append(match)

    outside_declared_range = protocol_has_declared_range and not own_ranges

    if conflicts:
        status = STATUS_CONFLICT
        owners = ", ".join(f"{m.av_protocol} {m.cidr}" for m in conflicts)
        detail = (
            f"{target} collides with {len(conflicts)} exclusive range(s) "
            f"reserved for another protocol: {owners}"
        )
    elif advisories or outside_declared_range:
        status = STATUS_INFORMATIONAL
        notes: list[str] = []
        if advisories:
            shared = ", ".join(f"{m.av_protocol} {m.cidr}" for m in advisories)
            notes.append(f"shares space with {shared} (range not marked exclusive)")
        if outside_declared_range:
            notes.append(f"sits outside every range declared for {av_protocol} in this space")
        detail = f"{target}: " + "; ".join(notes)
    else:
        status = STATUS_OK
        detail = (
            f"{target} sits inside the declared {av_protocol} range(s)"
            if own_ranges
            else f"No reserved range in this space covers {target}"
        )

    return AllocationConflictReport(
        space_id=space_id,
        target=str(target),
        av_protocol=av_protocol,
        status=status,
        conflicts=conflicts,
        advisories=advisories,
        own_ranges=own_ranges,
        outside_declared_range=outside_declared_range,
        # Only offered when there's something to act on — a "suggested
        # range" next to a green banner is noise.
        suggested_range=(suggest_default_range(av_protocol) if status != STATUS_OK else None),
        detail=detail,
    )


def suggest_default_range(av_protocol: str) -> str | None:
    """The well-known default range for ``av_protocol``, if one exists.

    ``None`` is a real answer, not a miss: SMPTE ST 2110 defines no
    range (plants carve their own), NDI is not multicast-addressed by
    default, and ``other`` is by definition site-specific. Those are
    exactly the deployments the operator-declared model was built for.

    Never auto-applied — a vendor default is something an operator may
    have deliberately moved off, so this only ever seeds a form.
    """
    return AV_DEFAULT_RANGES.get(av_protocol)


__all__ = [
    "RELATION_INSIDE",
    "RELATION_OVERLAPS",
    "STATUS_CONFLICT",
    "STATUS_INFORMATIONAL",
    "STATUS_OK",
    "AllocationConflictReport",
    "IPNetwork",
    "ReservedRangeMatch",
    "check_allocation_conflict",
    "parse_target",
    "resolve_range_for_address",
    "suggest_default_range",
]
