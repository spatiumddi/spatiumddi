"""BGP Looking Glass — IPAM / ASN / VRF linkage resolver (issue #566 Phase 3).

Resolves a learned route's ``(prefix, origin_asn)`` against the IPAM tree so
``BGPLGRoute.matched_{block,subnet,space,asn,vrf}_id`` can be populated — both
at ingest time (``routes_ingest.py``) and by the periodic re-resolve sweep
(``app.tasks.looking_glass``) that catches IPAM edits made between RIB
pushes.

**Semantics (read before changing anything):**

* ``matched_subnet_id`` wins over ``matched_block_id`` when both could
  match — a subnet is always the more specific/leaf entity. If a subnet
  matches, ``matched_block_id`` is read straight off ``subnet.block_id``
  (no separate block LPM needed).
* Containment direction: a route's ``prefix`` matches an IPAM object when
  the IPAM object's CIDR is a supernet of, or equal to, the route prefix
  (``target.subnet_of(net)``) — i.e. the route is same-or-more-specific
  than the IPAM object. An aggregate route covering several IPAM subnets
  (e.g. advertising a whole ``/16`` containing many ``/24`` subnets) can
  match an enclosing **block** but no single subnet — that's intentional,
  an aggregate doesn't "belong to" one leaf subnet.
* ``matched_vrf_id`` is NOT RD-based VRF matching. ``ext_communities``
  (which would carry RD/RT for a real VPNv4/VPNv6 match) is an explicitly
  deferred later phase — there is no VPN AFI/SAFI data to parse yet.
  ``matched_vrf_id`` here means "the VRF assigned to whichever IPAM
  block/space this prefix falls under" (first non-NULL ``vrf_id`` walking
  block -> parent_block_id chain -> space), the only VRF signal available
  pre-VPNv4. A future VPNv4 phase must not assume this field already means
  RD-matched.
* ``matched_asn_id`` is a raw ``origin_asn == ASN.number`` match, NOT "the
  ASN assigned to the matched IPAM object." A route whose origin AS has no
  tracked ``ASN`` row gets ``matched_asn_id = None`` even if its prefix
  falls inside a block that itself has an ``asn_id`` — these are two
  different concepts (who announced it vs. who's supposed to own the
  address space) and only the former is resolved here.

**Why a TTL cache (mirrors ``app.services.rpki_roa``):** each collector
pushes one full-snapshot POST per peer roughly every 30s (jittered). A
deployment with N peers calls ``ingest_routes()`` roughly N times per 30s
window, so rebuilding the full subnet+block+space+ASN scan on every single
call scales with peer count for no benefit — IPAM structure doesn't change
every 30 seconds. The 15s TTL cache caps rebuilds at ~2 per 30s window
regardless of peer count while staying fresh enough for the common case;
the periodic re-resolve sweep is the correctness backstop for the rest.
"""

from __future__ import annotations

import ipaddress
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asn import ASN
from app.models.ipam import IPBlock, IPSpace, Subnet

_CACHE_TTL_SECONDS = 15.0  # mirrors rpki_roa.py's _CACHE_TTL_SECONDS pattern

_cache: tuple[float, LinkResolutionCache] | None = None


def _parse_net(value: str | None) -> ipaddress._BaseNetwork | None:
    if not value:
        return None
    try:
        return ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        return None


@dataclass(frozen=True)
class ResolvedLinks:
    block_id: uuid.UUID | None = None
    subnet_id: uuid.UUID | None = None
    space_id: uuid.UUID | None = None
    asn_id: uuid.UUID | None = None
    vrf_id: uuid.UUID | None = None


@dataclass
class LinkResolutionCache:
    subnets: list[tuple[Subnet, ipaddress._BaseNetwork]] = field(default_factory=list)
    blocks: list[tuple[IPBlock, ipaddress._BaseNetwork]] = field(default_factory=list)
    blocks_by_id: dict[uuid.UUID, IPBlock] = field(default_factory=dict)
    spaces_by_id: dict[uuid.UUID, IPSpace] = field(default_factory=dict)
    asn_by_number: dict[int, uuid.UUID] = field(default_factory=dict)


async def build_resolution_cache(db: AsyncSession) -> LinkResolutionCache:
    """Uncached — always hits the DB. Use ``get_resolution_cache()`` for the
    TTL-cached wrapper; call this directly only from the periodic re-resolve
    sweep, which wants a fresh read every time it runs."""
    cache = LinkResolutionCache()

    for s in (await db.execute(select(Subnet))).scalars().all():
        net = _parse_net(str(s.network))
        if net is not None:
            cache.subnets.append((s, net))

    all_blocks = (await db.execute(select(IPBlock))).scalars().all()
    for b in all_blocks:
        cache.blocks_by_id[b.id] = b
        net = _parse_net(str(b.network))
        if net is not None:
            cache.blocks.append((b, net))

    for sp in (await db.execute(select(IPSpace))).scalars().all():
        cache.spaces_by_id[sp.id] = sp

    for number, asn_id in (await db.execute(select(ASN.number, ASN.id))).all():
        cache.asn_by_number[int(number)] = asn_id

    return cache


async def get_resolution_cache(db: AsyncSession) -> LinkResolutionCache:
    """TTL-cached wrapper — avoids a full subnet+block+space+ASN table scan
    on every single ~30s-per-peer RIB push (see module docstring). Mirrors
    ``app.services.rpki_roa``'s ``_get_cached_roas()`` pattern exactly."""
    global _cache
    now = time.monotonic()
    if _cache is not None and (now - _cache[0]) < _CACHE_TTL_SECONDS:
        return _cache[1]
    fresh = await build_resolution_cache(db)
    _cache = (now, fresh)
    return fresh


def _best_containing(
    candidates: list[tuple],
    target: ipaddress._BaseNetwork,
):
    """Most-specific (longest-prefix) candidate whose network is a supernet
    of, or equal to, ``target``. Mirrors
    ``cloud/reconcile.py::_find_enclosing_block``'s ``net.subnet_of()``
    check, generalised over both ``Subnet`` and ``IPBlock`` candidate
    lists."""
    best = None
    best_prefixlen = -1
    for obj, net in candidates:
        if net.version != target.version:
            continue
        try:
            contains = target.subnet_of(net)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            contains = False
        if contains and net.prefixlen > best_prefixlen:
            best = obj
            best_prefixlen = net.prefixlen
    return best


def _effective_vrf_id(block_id: uuid.UUID, cache: LinkResolutionCache) -> uuid.UUID | None:
    """First non-NULL ``vrf_id`` walking ``block -> parent_block_id`` chain
    -> space. No inherit-toggle exists on ``IPBlock``/``IPSpace`` for
    ``vrf_id``/``asn_id`` (unlike DDNS's ``ddns_inherit_settings``) — first
    non-NULL wins."""
    start = cache.blocks_by_id.get(block_id)
    if start is None:
        return None
    space_id = start.space_id
    seen: set[uuid.UUID] = set()
    current: IPBlock | None = start
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.vrf_id is not None:
            return current.vrf_id
        current = (
            cache.blocks_by_id.get(current.parent_block_id) if current.parent_block_id else None
        )
    space = cache.spaces_by_id.get(space_id)
    return space.vrf_id if space is not None else None


def resolve_route_links(
    cache: LinkResolutionCache, prefix: str, origin_asn: int | None
) -> ResolvedLinks:
    """Pure, in-memory match against a pre-built cache. Called once per
    route inside ``ingest_routes()``'s upsert loop and once per active
    route inside the periodic re-resolve sweep."""
    try:
        target = ipaddress.ip_network(str(prefix), strict=False)
    except ValueError:
        return ResolvedLinks()

    subnet = _best_containing(cache.subnets, target)
    block_id: uuid.UUID | None
    subnet_id: uuid.UUID | None = None
    space_id: uuid.UUID | None = None

    if subnet is not None:
        subnet_id = subnet.id
        block_id = subnet.block_id
        space_id = subnet.space_id
    else:
        block = _best_containing(cache.blocks, target)
        block_id = block.id if block else None
        space_id = block.space_id if block else None

    vrf_id = _effective_vrf_id(block_id, cache) if block_id is not None else None
    asn_id = cache.asn_by_number.get(int(origin_asn)) if origin_asn is not None else None

    return ResolvedLinks(
        block_id=block_id,
        subnet_id=subnet_id,
        space_id=space_id,
        asn_id=asn_id,
        vrf_id=vrf_id,
    )


def _clear_cache_for_test() -> None:
    global _cache
    _cache = None


__all__ = [
    "ResolvedLinks",
    "LinkResolutionCache",
    "build_resolution_cache",
    "get_resolution_cache",
    "resolve_route_links",
    "_clear_cache_for_test",
]
