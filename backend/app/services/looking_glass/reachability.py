"""Read-only BGP reachability helpers (issue #566 Phase 6).

1. ``find_covering_routes`` — longest-prefix-match against the learned RIB
   for a single IP. Hoisted out of
   ``app.services.ai.tools.bgp_lg.find_bgp_route_for_ip`` (its only caller
   before this phase, sharing an implementation with
   ``app.services.looking_glass.reverse_lookup.best_route_for_ip`` which the
   ``GET /looking-glass/routes/for-ip`` endpoint uses) so the multicast
   cross-reference below can share the identical LPM + tie-break semantics
   instead of a third hand-rolled copy.
2. ``multicast_bgp_reachability`` — cross-references every PIM domain's
   rendezvous-point address and every multicast group's producer source
   subnet against the learned RIB. Computed on demand, nothing persisted —
   a much looser/lighter signal than VRF RT matching (``vrf_match.py``),
   not worth a schema change.
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bgp_looking_glass import BGPLGRoute
from app.models.ipam import IPAddress, Subnet
from app.models.multicast import MulticastDomain, MulticastGroup, MulticastMembership
from app.models.network import NetworkDevice


async def _covering_candidates(
    db: AsyncSession, network_or_ip: str
) -> list[tuple[Any, BGPLGRoute]]:
    stmt = select(BGPLGRoute).where(
        BGPLGRoute.prefix.op(">>=")(network_or_ip),
        BGPLGRoute.withdrawn_at.is_(None),
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: list[tuple[Any, BGPLGRoute]] = []
    for row in rows:
        try:
            net = ipaddress.ip_network(str(row.prefix), strict=False)
        except ValueError:
            continue
        out.append((net, row))
    return out


def _sort_candidates(candidates: list[tuple[Any, BGPLGRoute]]) -> list[BGPLGRoute]:
    # Longest prefix wins; among ties prefer the best path, then order by
    # peer id so the result never depends on iteration/DB order (mirrors
    # reverse_lookup.best_route_for_ip's tie-break).
    candidates.sort(key=lambda t: (-t[0].prefixlen, 0 if t[1].is_best else 1, str(t[1].peer_id)))
    return [row for _net, row in candidates]


async def find_covering_routes(db: AsyncSession, ip: str) -> list[BGPLGRoute]:
    """Every active route covering a single IP, longest-prefix-first."""
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return []
    candidates = [
        (net, row)
        for net, row in await _covering_candidates(db, str(ip_obj))
        if net.version == ip_obj.version and ip_obj in net
    ]
    return _sort_candidates(candidates)


async def find_covering_route_for_subnet(db: AsyncSession, subnet_cidr: str) -> BGPLGRoute | None:
    """Best active route that is a supernet-or-equal of ``subnet_cidr`` —
    i.e. the WHOLE subnet is reachable, not just its base address.
    Deliberately distinct from ``find_covering_routes``'s single-IP
    semantics: a /32 host route sitting on the subnet's network address
    would satisfy a plain point LPM without covering the rest of the
    subnet, which would be a wrong answer for "is this source subnet
    reachable?".
    """
    try:
        subnet_net = ipaddress.ip_network(subnet_cidr, strict=False)
    except ValueError:
        return None
    candidates = [
        (net, row)
        for net, row in await _covering_candidates(db, str(subnet_net))
        if net.version == subnet_net.version and net.prefixlen <= subnet_net.prefixlen
    ]
    sorted_routes = _sort_candidates(candidates)
    return sorted_routes[0] if sorted_routes else None


@dataclass
class DomainReachabilityResult:
    domain_id: uuid.UUID
    domain_name: str
    rp_address: str
    covering_route: BGPLGRoute | None


@dataclass
class GroupReachabilityResult:
    group_id: uuid.UUID
    group_name: str
    group_address: str
    source_subnet_id: uuid.UUID
    source_subnet: str
    covering_route: BGPLGRoute | None


@dataclass
class MulticastReachabilityResult:
    domains: list[DomainReachabilityResult]
    groups: list[GroupReachabilityResult]


async def _resolve_domain_rp(db: AsyncSession, domain: MulticastDomain) -> str | None:
    if domain.rendezvous_point_address:
        return domain.rendezvous_point_address
    if domain.rendezvous_point_device_id:
        device = await db.get(NetworkDevice, domain.rendezvous_point_device_id)
        if device is not None:
            return str(device.ip_address)
    return None


async def multicast_bgp_reachability(db: AsyncSession) -> MulticastReachabilityResult:
    """PIM-domain RP reachability + multicast-group producer-subnet
    reachability against the learned RIB, in one call.

    Only domains in ``pim_mode`` ``sparse``/``bidir`` are RP-reachability
    checked — those are the only modes where an RP is meaningful
    (``MulticastDomain``'s own docstring in ``app.models.multicast``).
    """
    domain_rows = (
        (
            await db.execute(
                select(MulticastDomain).where(MulticastDomain.pim_mode.in_(("sparse", "bidir")))
            )
        )
        .scalars()
        .all()
    )
    domains: list[DomainReachabilityResult] = []
    for d in domain_rows:
        rp = await _resolve_domain_rp(db, d)
        if not rp:
            continue
        routes = await find_covering_routes(db, rp)
        domains.append(
            DomainReachabilityResult(
                domain_id=d.id,
                domain_name=d.name,
                rp_address=rp,
                covering_route=routes[0] if routes else None,
            )
        )

    # Producer memberships -> IPAddress -> enclosing Subnet. A group with
    # multiple producers in different subnets yields multiple rows (each
    # subnet checked once); dedupe (group, subnet) pairs.
    group_rows = (
        await db.execute(
            select(MulticastGroup, Subnet)
            .join(MulticastMembership, MulticastMembership.group_id == MulticastGroup.id)
            .where(MulticastMembership.role == "producer")
            .join(IPAddress, IPAddress.id == MulticastMembership.ip_address_id)
            .join(Subnet, Subnet.id == IPAddress.subnet_id)
        )
    ).all()
    groups: list[GroupReachabilityResult] = []
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for group, subnet in group_rows:
        key = (group.id, subnet.id)
        if key in seen:
            continue
        seen.add(key)
        route = await find_covering_route_for_subnet(db, str(subnet.network))
        groups.append(
            GroupReachabilityResult(
                group_id=group.id,
                group_name=group.name,
                group_address=str(group.address),
                source_subnet_id=subnet.id,
                source_subnet=str(subnet.network),
                covering_route=route,
            )
        )

    return MulticastReachabilityResult(domains=domains, groups=groups)


__all__ = [
    "DomainReachabilityResult",
    "GroupReachabilityResult",
    "MulticastReachabilityResult",
    "find_covering_route_for_subnet",
    "find_covering_routes",
    "multicast_bgp_reachability",
]
